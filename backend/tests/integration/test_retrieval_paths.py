"""两条召回路径的返回**顺序**（§3.3.4 / §3.6）。

⚠️ 2026-10-05 实测发现的缺陷（本文件是它的回归锁）：

   `bm25_retrieve()` 先算出 BM25 排序，再去 `collection.get(ids=[...])` 取正文，
   然后**按 Chroma 返回的顺序**做 `folded[:k]` 截断 ——
   而 Chroma 的 `get` **不保证**按传入 ids 的顺序返回（实测：传入顺序是
   [分数最高的, 次高的]，返回顺序是反的）。

   于是 BM25 算出来的排序被整条丢掉，这一路实际返回的是「任意 K 条」。
   实测现场：某 chunk 以 4.69 分排第一，却**不在最终返回的 10 条里** ——
   表现为「混合检索里 BM25 那一路好像没起作用」，且不报错。

⚠️ 这类错误不会崩、不会告警，只会让 Recall 变差，所以必须用**顺序**断言锁死。
"""

from __future__ import annotations

import uuid

import pytest_asyncio

from app import db
from app.retrieval.search import bm25_retrieve
from tests.support import delete_admin, ingest_text_doc, insert_admin, make_user, purge_documents

# 合成语料里唯一出现的短语 —— 保证 query 必然命中，断言才有意义
MARKER = "量子奶酪"


def _body(tag: str) -> str:
    return (
        f"【检索顺序测试文档 {tag}】\n\n"
        f"本文包含唯一关键词「{MARKER}」，用于验证 BM25 路的返回顺序。\n"
        f"再次出现「{MARKER}」以便分块后仍然集中。\n"
        + "本段是为了凑足分块长度而重复的正文，不含其他关键词。\n" * 12
    )


@pytest_asyncio.fixture
async def sample_doc():
    admin_id = await insert_admin()
    title = f"检索顺序_{uuid.uuid4().hex[:8]}"
    doc_id = await ingest_text_doc(admin_id=admin_id, title=title, text=_body(title))
    yield {"title": title, "document_id": doc_id}
    await purge_documents([title])
    await delete_admin(admin_id)


async def _bm25(query: str):
    async with db.tx() as conn:
        return await bm25_retrieve(conn, query, make_user("student"))


async def test_bm25_path_returns_top_scoring_chunk_first(sample_doc):
    """★ 最强命中必须排第一 —— 而不是被 Chroma 的存储顺序挤掉。

    修复前的失败形态：这个 chunk 以最高分命中，却**根本不在返回结果里**。

    两条断言缺一不可：
    - 「排第一是谁」是功能性判据，修复前直接失败；
    - 「分数降序」是顺序判据。单看后者会**假通过** —— 修复前返回的 10 条恰好
      全是 0 分（真正命中的那条被挤掉了），`[0.0]*10` 本身就是降序。
    """
    chunks = await _bm25(MARKER)

    assert chunks, "BM25 路一条都没返回"
    assert chunks[0].document_id == sample_doc["document_id"], (
        f"排第一的不是命中该关键词的文档，而是 {chunks[0].document_id}"
    )

    scores = [c.score for c in chunks]
    assert scores == sorted(scores, reverse=True), (
        f"BM25 路返回顺序不是分数降序：{scores}"
    )
