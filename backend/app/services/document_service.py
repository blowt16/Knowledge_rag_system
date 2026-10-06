"""文档上传与进度（§3.7.2 / §3.7.3）。

**分层**（方案 §3.1）：`api/documents.py` 只做参数校验与响应封装，业务在这里。

**上传粒度**：一次 zip = 一个 `batch_id` + N 条 `ingestion_tasks`（每条对应包内一个文件）。
单文件上传时 `batch_id = task_id`、`tasks` 只有一项 —— **响应结构统一**，
前端不必写两套。

⚠️ M0 只实现单文件。zip 批量与 `upload/batch/{batch_id}/stream` 在 M4 补
   （只影响管理端体验，不阻塞端到端）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from app import db
from app.core.config import repo_path
from app.core.exceptions import AppError, NotFound
from app.core.telemetry import reattach
from app.ingestion.pipeline import IngestRequest, ingest
from app.retrieval import vector
from app.services import index_service

logger = logging.getLogger(__name__)

# `counts` 的键**恰好是 ingestion_tasks.status 的 7 个取值**，一个不多一个不少
TASK_STATUSES = ["pending", "parsing", "chunking", "embedding",
                 "done", "duplicate", "failed"]

TERMINAL_STATUSES = {"done", "duplicate", "failed"}


def counts_for(status: str) -> dict[str, int]:
    """进度流 `counts` 载荷。键集必须与 `ingestion_tasks.status` 完全一致。"""
    return {k: (1 if k == status else 0) for k in TASK_STATUSES}


def _tmp_path(task_id: str, filename: str) -> Path:
    r"""临时落盘路径。

    ⚠️ **文件名来自 multipart，必须净化**（评审 I-3 实测）：原样拼进去时
       `Path("data/tmp") / "TASK_..\..\..\..\evil.txt"` 会解析到
       **仓库根目录**，而 `write_bytes` 发生在入库**之前** ——
       等于「上传即可往任意路径写文件」，且落在下次启动会加载的位置。
       `{task_id}_` 前缀只挡住第一段，`..` 照样往上跳。

    只取最后一段、去掉前导点、两个方向的分隔符都当分隔符处理
       （反斜杠在 POSIX 上不是分隔符，但不能因此把它当普通字符放行）。
    """
    tmp_dir = repo_path("data", "tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    safe = base.lstrip(".") or "unnamed"
    return tmp_dir / f"{task_id}_{safe}"


def normalize_roles(visible_roles: str, visibility: str) -> list[str]:
    """「受限」至少要有角色 —— 一个都不勾的话谁也看不到，且不会报错。"""
    roles = [r.strip() for r in (visible_roles or "").split(",") if r.strip()]
    if visibility == "restricted" and not roles:
        roles = ["admin"]
    return roles


def parse_effective_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def create_upload(
    *,
    filename: str,
    content: bytes,
    uploader_id: str,
    trace_id: str,
    title: str | None,
    visibility: str,
    visible_roles: str,
    effective_date: str | None,
) -> tuple[str, str]:
    """落地临时文件 + 建任务行。返回 (task_id, batch_id)。

    单文件时 batch_id = task_id，口径与 zip 批量统一。
    """
    task_id = uuid.uuid4().hex
    batch_id = task_id

    tmp_path = _tmp_path(task_id, filename)
    tmp_path.write_bytes(content)

    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (id, batch_id, file_name, status, progress, trace_id, uploader_id)
               VALUES ($1,$2,$3,'pending',0,$4,$5)""",
            task_id, batch_id, filename, trace_id, uploader_id,
        )

    return task_id, batch_id


def build_request(
    *,
    task_id: str,
    batch_id: str,
    filename: str,
    uploader_id: str,
    trace_id: str,
    title: str | None,
    visibility: str,
    visible_roles: str,
    effective_date: str | None,
) -> IngestRequest:
    return IngestRequest(
        source_path=_tmp_path(task_id, filename),
        filename=filename,
        uploader_id=uploader_id,
        task_id=task_id,
        batch_id=batch_id,
        trace_id=trace_id,
        title=title,
        visibility=visibility if visibility in ("public", "restricted") else "public",
        visible_roles=normalize_roles(visible_roles, visibility),
        effective_date=parse_effective_date(effective_date),
    )


def schedule_ingest(req: IngestRequest, *, trace_id: str, span_id: str) -> None:
    """把摄入丢到后台跑。

    ⚠️ 后台任务不能靠 contextvars 隐式继承 —— trace_id 已落库，
       这里显式挂回来（§3.2.3.1）。

    ⚠️ M0 用进程内 asyncio 任务；M4 换成由 `ingestion_tasks` 表驱动的 worker。
       进度流已按表实现，届时接口不变。
    """

    async def _run() -> None:
        with reattach(trace_id, span_id):
            try:
                await ingest(req)
            finally:
                req.source_path.unlink(missing_ok=True)

    asyncio.create_task(_run())


async def task_exists(task_id: str) -> bool:
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM ingestion_tasks WHERE id = $1", task_id)
    return row is not None


async def stream_progress(task_id: str, *, interval: float = 0.5) -> AsyncIterator[str]:
    """单文件上传进度 SSE。

    ⚠️ 这是与 chat 流**互相独立的一套协议**，不要复用 chat 的事件语义：
       chat 的 `error` 是**终止事件**，而这里单个文件失败**不影响整包继续**。

    ⚠️ 流的结束条件：整包不再有进行中的状态。`duplicate` 与 `failed` **都算终态**，
       不会被它们拖住（否则一次失败会让流永远挂着）。
    """
    last_status: str | None = None
    while True:
        async with db.tx() as conn:
            task = await conn.fetchrow(
                """SELECT status, progress, message FROM ingestion_tasks
                    WHERE id = $1""",
                task_id,
            )
        if task is None:
            break

        if task["status"] != last_status:
            last_status = task["status"]
            payload = {
                "done": 1 if status_is_done(task["status"]) else 0,
                # ⚠️ total 是**包内文件数**；单文件时恒为 1，
                #    进度百分比会长时间停在 0% 或 100% ——
                #    所以前端**必须**同时显示 current_file 作为兜底
                "total": 1,
                "current_file": task["message"] or "",
                "counts": counts_for(task["status"]),
            }
            yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if task["status"] in TERMINAL_STATUSES:
            yield ("event: done\ndata: "
                   + json.dumps({"batch_id": task_id,
                                 "counts": counts_for(task["status"])},
                                ensure_ascii=False) + "\n\n")
            break

        await asyncio.sleep(interval)


def status_is_done(status: str) -> bool:
    return status == "done"


# ============================================================
# 管理端：列表 / 版本 / 分块预览 / 改字段 / 删除（§4.3.1 / §4.3.2）
# ============================================================

# 「当前生效版本」的判定 —— 与**检索期的版本折叠同一口径**（§3.3.1）：
# 组内 `status='active'` 且 `effective_date <= 今天` 中 version 最大的那一版。
# ⚠️ 两处口径必须一致：管理端高亮的「当前生效」和检索实际返回的必须是同一版，
#    否则管理员看着 v2 高亮、学生检索到的却是 v4。
_CURRENT_VERSION_SQL = """
    d.status = 'active'
    AND d.effective_date <= CURRENT_DATE
    AND d.version = (
        SELECT max(d2.version) FROM documents d2
         WHERE d2.doc_group_id = d.doc_group_id
           AND d2.status = 'active' AND d2.effective_date <= CURRENT_DATE
    )
"""

# 改了这些字段就要**重写 Chroma 的 chunk metadata**（§4.3.1.2）——
# 它们冗余在向量库里，改 PG 不会自动同步，而检索期的过滤读的正是 Chroma。
# ⚠️ `title` 不在此列：它只是显示名，重索引的代价不该为一个改名而付。
REINDEX_FIELDS = ("visibility", "visible_roles", "effective_date", "status")


def _doc_item(row) -> dict:
    roles = row["visible_roles"] or []
    if isinstance(roles, str):        # 防御：jsonb 编解码器缺失时会是字符串
        try:
            roles = json.loads(roles)
        except (ValueError, TypeError):
            roles = []
    return {
        "id": row["id"],
        "doc_group_id": row["doc_group_id"],
        "title": row["title"],
        "filename": row["filename"],
        "file_type": row["file_type"],
        "version": row["version"],
        "status": row["status"],
        "visibility": row["visibility"],
        "visible_roles": list(roles),
        "effective_date": (row["effective_date"].isoformat()
                           if row["effective_date"] else None),
        "chunk_count": row["chunk_count"],
        "created_at": (row["created_at"].isoformat() if row["created_at"] else None),
        "is_current": bool(row.get("is_current")),
    }


async def list_documents(*, status: str = "all", visibility: str = "all",
                         q: str = "", page: int = 1,
                         page_size: int = 20) -> dict:
    """管理端文档列表（筛选参数见 §4.3.1.1）。

    `status` / `visibility` 都支持 `all`（默认）；`q` 模糊匹配**标题与原始文件名**。
    """
    where, args = ["TRUE"], []

    def _add(clause: str, value) -> None:
        args.append(value)
        where.append(clause.format(n=len(args)))

    if status != "all":
        _add("d.status = ${n}", status)
    if visibility != "all":
        _add("d.visibility = ${n}", visibility)
    if q:
        args.append(f"%{q}%")
        # 同一个参数用两次，所以只 append 一次、占位符写两遍
        where.append(f"(d.title ILIKE ${len(args)} OR d.filename ILIKE ${len(args)})")

    clause = " AND ".join(where)
    async with db.tx() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM documents d WHERE {clause}", *args)
        rows = await conn.fetch(
            f"""SELECT d.*, ({_CURRENT_VERSION_SQL}) AS is_current
                  FROM documents d
                 WHERE {clause}
                 ORDER BY d.created_at DESC, d.version DESC
                 OFFSET ${len(args) + 1} LIMIT ${len(args) + 2}""",
            *args, (page - 1) * page_size, page_size)

    return {
        "items": [_doc_item(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    }


async def get_versions(group_id: str) -> dict:
    """同组的全部版本，**倒序**（当前生效在最上面，供「版本管理」折叠展示）。"""
    async with db.tx() as conn:
        rows = await conn.fetch(
            f"""SELECT d.*, ({_CURRENT_VERSION_SQL}) AS is_current
                  FROM documents d
                 WHERE d.doc_group_id = $1
                 ORDER BY d.version DESC""",
            group_id)
    if not rows:
        raise NotFound("文档组不存在")
    return {"doc_group_id": group_id, "versions": [_doc_item(r) for r in rows]}


async def get_chunks_preview(document_id: str, *, page: int = 1,
                             page_size: int = 20) -> dict:
    """分块预览（§4.3.2）—— **只读预览，不做编辑**。

    编辑 chunk 正文意味着重新嵌入该 chunk 并同步两处存储，属独立特性，本轮不做。
    这是目前**唯一**能看见 `char_start/char_end`、`current_chapter`、`vis_*`
    这些 metadata 的地方 —— 排查「检索为什么没召回」的第一手段。
    """
    async with db.tx() as conn:
        exists = await conn.fetchval("SELECT 1 FROM documents WHERE id = $1", document_id)
    if not exists:
        raise NotFound("文档不存在")

    rows = vector.get_chunks(document_id, limit=100000)
    start = (page - 1) * page_size
    chunks = []
    for row in rows[start:start + page_size]:
        meta = row["metadata"]
        paths = meta.get("image_paths") or []
        if isinstance(paths, str):
            try:
                paths = json.loads(paths)
            except (ValueError, TypeError):
                paths = []
        chunks.append({
            "chunk_index": int(meta.get("chunk_index", 0)),
            "page": int(meta.get("page", 1) or 1),
            "current_chapter": meta.get("current_chapter") or "",
            "chapter_level": int(meta.get("chapter_level", 0) or 0),
            "char_start": int(meta.get("char_start", 0) or 0),
            "char_end": int(meta.get("char_end", 0) or 0),
            "text": row["text"],
            "image_paths": list(paths),
        })
    return {
        "chunks": chunks,
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < len(rows),
    }


async def update_document(document_id: str, *, title: str | None = None,
                          visibility: str | None = None,
                          visible_roles: list[str] | None = None,
                          effective_date: str | None = None,
                          status: str | None = None) -> dict:
    """改可改字段（§3.7.2 侧的管理端写接口规格）。

    ⚠️ **顺序是先 Chroma、后 PG**：两处存储做不到原子，那么失败时应该
       停在**更保守**的那一侧。先写 Chroma 意味着最坏情况是
       「向量库已经按新权限过滤了、PG 还没更新」—— 偏严；反过来则是
       「PG 说受限、向量库还公开着」—— **那是泄露**。
    """
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT * FROM documents WHERE id = $1", document_id)
    if row is None:
        raise NotFound("文档不存在")

    new_visibility = visibility if visibility is not None else row["visibility"]
    new_roles = (list(visible_roles) if visible_roles is not None
                 else list(row["visible_roles"] or []))
    # 与上传路径同一口径：「受限」至少要有一个角色，否则谁也看不到且不报错
    new_roles = normalize_roles(",".join(new_roles), new_visibility)

    if effective_date is not None:
        parsed = parse_effective_date(effective_date)
        if parsed is None:
            raise AppError("effective_date 格式应为 YYYY-MM-DD")
        new_effective = parsed
    else:
        new_effective = row["effective_date"]

    new_status = status if status is not None else row["status"]

    changed = {
        "title": title is not None and title != row["title"],
        "visibility": new_visibility != row["visibility"],
        "visible_roles": sorted(new_roles) != sorted(row["visible_roles"] or []),
        "effective_date": new_effective != row["effective_date"],
        "status": new_status != row["status"],
    }

    if any(changed[f] for f in REINDEX_FIELDS):
        # 先重写向量库：这三个字段冗余在 Chroma 里，检索期的过滤读的是它
        index_service.rewrite_metadata(
            document_id,
            status=new_status,
            visibility=new_visibility,
            visible_roles=new_roles,
            effective_date=new_effective.isoformat(),
        )

    async with db.tx() as conn:
        # ⚠️ `visible_roles` 传 **list** 而不是 json.dumps 后的字符串：
        #    连接池给 jsonb 注册了编解码器（db.py），再传字符串会被**二次编码**
        #    成一个 JSON 字符串标量 —— 读回来就不是 list 了。
        await conn.execute(
            """UPDATE documents
                  SET title = $2, visibility = $3, visible_roles = $4::jsonb,
                      effective_date = $5, status = $6, updated_at = now()
                WHERE id = $1""",
            document_id, title if title is not None else row["title"],
            new_visibility, new_roles, new_effective, new_status,
        )
        updated = await conn.fetchrow(
            f"""SELECT d.*, ({_CURRENT_VERSION_SQL}) AS is_current
                  FROM documents d WHERE d.id = $1""",
            document_id)
    return _doc_item(updated)


async def set_document_status(document_id: str, status: str) -> dict:
    """启用 / 停用（§3.7.3）。

    ⚠️ 停用**当前生效版本**会让上一版「复活」（§3.3.1 的折叠规则）——
       管理端必须在按钮旁显式提示这一点，由前端负责（3b）。
    """
    return await update_document(document_id, status=status)


async def delete_document(document_id: str) -> None:
    """**真删**（M4-D3）：Chroma chunk + BM25 条目 + PG 行，**源文件与规范化文本保留**。

    依据是 §3.3.1 既有的版本语义「删掉新版即可，旧版自动恢复生效，不用手工 enable」——
    删除就该让同组上一版接管。源文件不删：删了既不能回滚、也再拿不到原文
    （与「破坏性操作先归档」的既有规矩一致）。

    ⚠️ 两张表有指向 `documents(id)` 的外键：`ingestion_tasks.document_id` 与
       `refusal_annotations.suggested_document_id`（§3.3.1）。任务行随文档一起删；
       标注行**保留**（那条标注本身是运维事实），只把指向本档的建议置空。
    """
    async with db.tx() as conn:
        exists = await conn.fetchval("SELECT 1 FROM documents WHERE id = $1", document_id)
    if not exists:
        raise NotFound("文档不存在")

    # ⚠️ **先把行落到 `disabled` 中间态**（评审 M4-K6）：原实现是
    #    「先删索引 → 再删 PG 行」，PG 那次 DELETE 若失败，行还显示 `active`、
    #    `chunk_count>0`，而内容其实已经检索不到了 —— **没有任何状态能标记
    #    这个不一致**，管理员看不出该重试。
    #    落到 `disabled` 之后：检索立刻停、管理端看得见、重试一次删除即可。
    async with db.tx() as conn:
        await conn.execute("UPDATE documents SET status = 'disabled' WHERE id = $1",
                           document_id)

    # 再删索引（内部已按「先取 chunk_id、再删 Chroma、后删 BM25」的正确顺序）。
    # 这一步失败 → 行停在 `disabled`，正是我们要的中间态。
    index_service.remove_from_index(document_id)

    async with db.tx() as conn:
        await conn.execute("DELETE FROM ingestion_tasks WHERE document_id = $1",
                           document_id)
        await conn.execute(
            "UPDATE refusal_annotations SET suggested_document_id = NULL "
            "WHERE suggested_document_id = $1", document_id)
        await conn.execute("DELETE FROM documents WHERE id = $1", document_id)

    logger.info("文档已删除", extra={"event": "document.deleted",
                                    "document_id": document_id})
