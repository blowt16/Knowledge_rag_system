"""检索编排：两路召回 + 版本折叠 + 固定过采样（§3.3.4 / §3.5.3 节点 6）。

完整链路固定为：
    召回 → 版本折叠 → RRF 融合 → 精排 → Top-5 进 build_context

⚠️ **两路的过滤位置不同，不能当成同一套机制**：

| | 过滤位置 | 是否需要过采样 |
|---|---|---|
| **向量** | 检索器内部（先 Top-N 再过滤） | ✅ 需要 |
| **BM25** | **全量打分 → 按文档粒度过滤 → 取 K** | ❌ 不需要 |

⚠️ **折叠必须回查 PostgreSQL**，不能在召回集内取最大（见 fusion.fold_versions）。

⚠️ 折叠作用于**两路候选的并集**：BM25 对措辞雷同的旧版命中率天然更高，
   若它不做折叠，旧版会以高 RRF 分进入候选并可能被返回。
"""

from __future__ import annotations

import json
import logging
from datetime import date

import asyncpg

from app.core.config import cfg
from app.core.deps import UserContext
from app.graph.state import Chunk
from app.retrieval import bm25, vector
from app.retrieval.embedding import embed_query
from app.retrieval.filters import allowed_document_ids, build_where

logger = logging.getLogger(__name__)


def _json_field(value, default):
    """Chroma metadata 里的数组/对象字段以 JSON **字符串**存放。

    ⚠️ `vector.query()` 会替调用方解析，但 `bm25_retrieve` 走的是
       `collection.get()`（原始 metadata）—— 那里拿到的是字符串。
       2026-10-05 实测踩过：`bbox` 是字符串时 `for raw in chunk.bbox`
       迭代的是**单个字符**，在 cite 节点炸出
       `AttributeError: 'str' object has no attribute 'get'`。
       **两路都必须归一化，不能指望调用方记得解析。**
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except (ValueError, TypeError):
            return default
    return value


def _to_chunk(hit: dict) -> Chunk:
    meta = hit.get("metadata") or {}
    return Chunk(
        chunk_id=hit.get("chunk_id", ""),
        text=hit.get("text", ""),
        document_id=meta.get("document_id", ""),
        doc_group_id=meta.get("doc_group_id", ""),
        version=int(meta.get("version", 0) or 0),
        chunk_index=int(meta.get("chunk_index", 0) or 0),
        page=int(meta.get("page", 1) or 1),
        char_start=int(meta.get("char_start", 0) or 0),
        char_end=int(meta.get("char_end", 0) or 0),
        chapter=meta.get("current_chapter", "") or "",
        chapter_level=int(meta.get("chapter_level", 0) or 0),
        images=_json_field(meta.get("image_paths"), []),
        bbox=_json_field(meta.get("bbox"), []),
    )


async def fill_document_titles(
    conn: asyncpg.Connection, chunks: list[Chunk]
) -> None:
    """补上文档标题 —— 引用面板要显示「《XX办法》」而不是一串 document_id。

    ⚠️ 标题**不冗余进 Chroma**（改了标题不该触发重索引，见 §3.7.2），
       所以只能在检索后回一次 PostgreSQL 取。
       2026-10-05 浏览器实测发现：不补的话引用面板显示的是
       `cca67a0eea4e45fa861665ebb3190415` 这样的裸 ID，用户完全看不懂。
    """
    if not chunks:
        return
    ids = {c.document_id for c in chunks if c.document_id and not c.document_name}
    if not ids:
        return
    rows = await conn.fetch(
        "SELECT id, title FROM documents WHERE id = ANY($1::text[])", list(ids))
    titles = {r["id"]: r["title"] for r in rows}
    for chunk in chunks:
        if not chunk.document_name:
            chunk.document_name = titles.get(chunk.document_id, "")


async def latest_versions(
    conn: asyncpg.Connection, group_ids: set[str], *, today: date | None = None
) -> dict[str, int]:
    """回查 PostgreSQL，取每个组「active 且 effective_date ≤ 今天」的 max(version)。

    ⚠️ 这一步**必须**回库：Chroma 做不到跨条目聚合，
       而「在召回集内取最大」会让已废止的旧版被当成现行版本返回（§3.3.1）。
    """
    if not group_ids:
        return {}
    day = today or date.today()
    rows = await conn.fetch(
        """SELECT doc_group_id, MAX(version) AS latest
             FROM documents
            WHERE doc_group_id = ANY($1::text[])
              AND status = 'active'
              AND effective_date <= $2
            GROUP BY doc_group_id""",
        list(group_ids),
        day,
    )
    return {row["doc_group_id"]: int(row["latest"]) for row in rows}


async def _filter_by_documents(
    conn: asyncpg.Connection, chunks: list[Chunk], user: UserContext,
    *, include_restricted: bool, today: date,
) -> list[Chunk]:
    """按文档粒度套用与向量路**同构**的过滤（BM25 侧用）。

    依赖 ``filters.allowed_document_ids`` —— 与向量路共用同一套判定，
    避免「向量路过滤了、BM25 路没过滤」这种最危险的偏差。
    """
    if not chunks:
        return []
    doc_ids = {c.document_id for c in chunks if c.document_id}
    if not doc_ids:
        return []
    rows = await conn.fetch(
        """SELECT id, status, effective_date, visibility, visible_roles
             FROM documents WHERE id = ANY($1::text[])""",
        list(doc_ids),
    )
    docs = [dict(r) for r in rows]
    where = build_where(user, today=today, include_restricted=include_restricted)
    allowed = allowed_document_ids(where, docs, today=today)

    out: list[Chunk] = []
    for chunk in chunks:
        if chunk.document_id in allowed:
            if include_restricted and user.role == "admin":
                row = next((d for d in docs if d["id"] == chunk.document_id), None)
                if row is not None and not _visible_normally(row, user):
                    chunk.escalated = True
            out.append(chunk)
    return out


def _visible_normally(doc: dict, user: UserContext) -> bool:
    if doc.get("visibility") == "public":
        return True
    roles = doc.get("visible_roles") or []
    if isinstance(roles, str):
        import json
        try:
            roles = json.loads(roles)
        except (ValueError, TypeError):
            roles = []
    return user.role in roles


async def vector_retrieve(
    conn: asyncpg.Connection, query_text: str, user: UserContext,
    *, include_restricted: bool = False, today: date | None = None,
) -> list[Chunk]:
    """向量路：固定过采样 + 一次重试（§3.3.4）。

    判据必须包含**版本折叠后**的条数，不能只看 Chroma 过滤结果 ——
    某制度有 8 个历史版本时 Top-30 可能被占满，折叠后只剩 2–3 条。
    """
    day = today or date.today()
    k = int(cfg("retrieval.k", 10))
    where = build_where(user, today=day, include_restricted=include_restricted)

    embedding = await embed_query(query_text)

    async def _attempt(multiplier: int) -> list[Chunk]:
        hits = vector.query(embedding, where=where, n_results=multiplier * k)
        chunks = [_to_chunk(h) for h in hits]
        groups = {c.doc_group_id for c in chunks if c.doc_group_id}
        latest = await latest_versions(conn, groups, today=day)
        return [c for c in chunks if not c.doc_group_id or c.version == latest.get(c.doc_group_id, c.version)]

    chunks = await _attempt(int(cfg("retrieval.oversample_first", 3)))
    if len(chunks) >= k:
        await fill_document_titles(conn, chunks)
        return chunks[:k]

    # 重试：**整体替换**首次结果（不是叠加），替换后**重新折叠**
    retry_chunks = await _attempt(int(cfg("retrieval.oversample_retry", 6)))
    logger.info("向量路召回不足触发重试", extra={
        "event": "retrieve.oversample_retry", "node": "retrieve",
    })
    await fill_document_titles(conn, retry_chunks)
    return retry_chunks[:k]


async def bm25_retrieve(
    conn: asyncpg.Connection, query_text: str, user: UserContext,
    *, include_restricted: bool = False, today: date | None = None,
) -> list[Chunk]:
    """BM25 路：全量打分 → 按文档粒度过滤 → 取 K。**不做过采样**。"""
    day = today or date.today()
    k = int(cfg("retrieval.k", 10))

    hits = bm25.search(query_text, k=k * 4)   # 多取一些，过滤后再截断
    if not hits:
        return []

    # 映射表：下标 → chunk_id（§3.6），再由 chunk 回查 Chroma 拿 metadata。
    # 这里为了拿到 document_id 等字段，仍走一次 Chroma 的 get。
    chunks: list[Chunk] = []
    collection = vector.get_collection()
    ids = [h.chunk_id for h in hits]
    try:
        fetched = collection.get(ids=ids, include=["metadatas", "documents"])
    except Exception:  # noqa: BLE001
        return []

    score_by_id = {h.chunk_id: h.score for h in hits}
    for i, chunk_id in enumerate(fetched.get("ids", [])):
        meta = (fetched.get("metadatas") or [])[i] or {}
        text = (fetched.get("documents") or [])[i] if fetched.get("documents") else ""
        chunk = _to_chunk({"chunk_id": chunk_id, "text": text, "metadata": meta})
        chunk.score = score_by_id.get(chunk_id, 0.0)
        chunks.append(chunk)

    chunks = await _filter_by_documents(
        conn, chunks, user, include_restricted=include_restricted, today=day)

    groups = {c.doc_group_id for c in chunks if c.doc_group_id}
    latest = await latest_versions(conn, groups, today=day)
    folded = [c for c in chunks
              if not c.doc_group_id or c.version == latest.get(c.doc_group_id, c.version)]

    await fill_document_titles(conn, folded)
    return folded[:k]
