"""测试共用夹具：造文档入库、按 title 清干净。

⚠️ 清理必须**同时**清 Chroma 与 BM25S。只清一处会往真实索引里漏死条目，
   每跑一次测试漏一批，而平时完全看不出来（不报错、只让召回变差）。
   2026-10-05 实测现场：映射表 186 条 / 31 个 document_id，而库里只有 1 个文档。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from app import db
from app.core.config import repo_path
from app.core.deps import UserContext
from app.ingestion.pipeline import IngestRequest, ingest
from app.retrieval import bm25, vector

logger = logging.getLogger(__name__)


def make_user(role: str) -> UserContext:
    return UserContext(id=f"u_{role}", username=role, role=role, token_version=0)


async def insert_admin() -> str:
    """插一个只用于挂 uploader_id 的管理员，返回 user_id。"""
    user_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, 'x', 'admin', 0)",
            user_id, f"t_{user_id[:8]}",
        )
    return user_id


async def delete_admin(user_id: str) -> None:
    """先清引用它的行，否则外键会拦住 users 的删除。"""
    async with db.tx() as conn:
        await conn.execute("DELETE FROM ingestion_tasks WHERE uploader_id = $1", user_id)
        await conn.execute("DELETE FROM documents WHERE uploader_id = $1", user_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def seed_task(req: IngestRequest) -> None:
    """`ingest()` 只 UPDATE 任务行、不 INSERT；不先种一行就没有进度可观测。"""
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (id, batch_id, file_name, status, progress, uploader_id, trace_id)
               VALUES ($1, $2, $3, 'pending', 0, $4, $5)""",
            req.task_id, req.batch_id, req.filename, req.uploader_id, req.trace_id,
        )


async def ingest_text_doc(
    *,
    admin_id: str,
    title: str,
    text: str,
    visibility: str = "public",
    visible_roles: list[str] | None = None,
    effective_date: date | None = None,
) -> str:
    """把一个字符串当成 .txt 入库，返回 document_id。

    用合成文本而不是语料 PDF：正文可控，query 就能**必然命中**，
    「过滤/排序失效」才会表现为「本该第一的没出现」，
    而不是「反正也排不进 Top-K」的假通过。入库也快得多。
    """
    path = Path(repo_path("data", "tmp")) / f"{uuid.uuid4().hex}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    task_id = uuid.uuid4().hex
    req = IngestRequest(
        source_path=path, filename=f"{title}.txt", uploader_id=admin_id,
        task_id=task_id, batch_id=task_id, trace_id="0" * 32,
        title=title, visibility=visibility, visible_roles=list(visible_roles or []),
        effective_date=effective_date or date(2020, 1, 1),
    )
    await seed_task(req)
    outcome = await ingest(req)
    assert outcome.status == "done", outcome.message
    return outcome.document_id


async def purge_documents_by_id(doc_ids: list[str]) -> None:
    """按 **document_id** 清掉测试造的文档：PG 两行 + 两处索引。

    ⚠️ 为什么要有按 id 的版本（而不是只有按 title 的）：**title 会被用例改掉**。
       实测：`test_patch_title_does_not_reindex` 把标题改成了「改过的标题」，
       而夹具按**原标题**清理 —— SELECT 查不到 → `doc_ids = []` →
       索引一条都没删，每跑一次就往真实索引里漏一条孤儿
       （连续跑几次后 Chroma 里躺着 4 条 `管理端测试_*`，
       把后续用例的 Top-K 挤掉，三个用例同时红在正控上）。
       按 id 清理不依赖任何会被改写的字段。
    """
    if not doc_ids:
        return
    async with db.tx() as conn:
        await conn.execute(
            "DELETE FROM ingestion_tasks WHERE document_id = ANY($1::text[])", doc_ids)
        await conn.execute("DELETE FROM documents WHERE id = ANY($1::text[])", doc_ids)
    for doc_id in doc_ids:
        try:
            # chunk_id 必须在删 Chroma **之前**取：删完就查不到了
            chunk_ids = [r["chunk_id"]
                         for r in vector.get_chunks(doc_id, limit=100000)]
            vector.delete_document(doc_id)
            bm25.remove_document(chunk_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("测试清理失败，索引里可能留下孤儿条目：document_id=%s（%s）",
                           doc_id, e, extra={"event": "test.purge_failed"})


async def purge_documents(titles: list[str]) -> None:
    """按 title 清理（先查 id 再走 `purge_documents_by_id`）。

    ⚠️ 只在**标题不会被用例改写**时可靠；会改名/改状态的用例请直接用
       `purge_documents_by_id`。
    """
    if not titles:
        return
    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id FROM documents WHERE title = ANY($1::text[])", titles)
    await purge_documents_by_id([r["id"] for r in rows])
