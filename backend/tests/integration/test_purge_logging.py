"""测试清理失败**必须留痕**（M1 的 K-9 / M2 的 K-5）。

⚠️ 为什么值得一条测试：清理失败**不会让用例变红** —— 它只会把死条目
   悄悄留进真实索引，表现为「召回慢慢变差」，而所有人都在看绿色的测试报告。
   M1 现场就是这么攒出来的：映射表 186 条 / 31 个 document_id，库里只有 1 个文档。

   原来的写法是 `except Exception: pass`，所以这条锁的是「别再吞回去」。
"""

from __future__ import annotations

import logging
import uuid

import pytest_asyncio

from app import db
from tests import support


@pytest_asyncio.fixture
async def seeded_doc():
    """直接插一行 documents（不走摄入，省掉 Chroma 写入），用完删掉。"""
    doc_id = uuid.uuid4().hex
    title = f"清理留痕测试_{doc_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO documents
               (id, doc_group_id, title, filename, file_type, md5, version,
                effective_date, status, visibility, chunk_count)
               VALUES ($1, $1, $2, 'x.md', 'md', $3, 1, CURRENT_DATE,
                       'active', 'public', 0)""",
            doc_id, title, doc_id,
        )
    yield doc_id, title
    async with db.tx() as conn:
        await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)


async def test_purge_failure_is_logged(seeded_doc, monkeypatch, caplog):
    doc_id, title = seeded_doc

    def boom(*a, **kw):
        raise RuntimeError("Chroma 被占用")
    monkeypatch.setattr(support.vector, "get_chunks", boom)

    with caplog.at_level(logging.WARNING, logger="tests.support"):
        await support.purge_documents([title])

    assert any("孤儿" in r.getMessage() for r in caplog.records), \
        "清理失败被吞了 —— 用例会全绿，而真实索引里留下孤儿条目"


async def test_purge_happy_path_does_not_warn(seeded_doc, monkeypatch, caplog):
    """反向锁：正常清理不该刷告警（否则告警又变成背景噪声）。"""
    doc_id, title = seeded_doc
    monkeypatch.setattr(support.vector, "get_chunks", lambda *a, **kw: [])

    with caplog.at_level(logging.WARNING, logger="tests.support"):
        await support.purge_documents([title])

    assert not [r for r in caplog.records if "孤儿" in r.getMessage()]
