"""检索期版本折叠（§3.3.1 / §3.5.3 节点 6）—— 计划 Task M1-3 的测试点。

验收原文：**造 v1/v2 同组文档，让 v1 的措辞与 query 更相似 → 断言返回的是 v2。**

⚠️ 为什么这条必须回查 PostgreSQL 而不是「在召回集里取最大」：
   若现行版 v2 的措辞与 query 不相似、而已废止的 v1 相似，Top-N 里可能**只有 v1**；
   在召回集内折叠就会把 v1 当成现行版本返回，用户拿到已废止的政策，
   界面还按「当前生效」展示 —— 亮点① 的版本隔离彻底失效。

⚠️ 本文件补的是**检索侧**的缺口：M0 只在**写入侧**验过版本
   （test_pipeline.py 断言「同 title 再传 → version=2，旧版仍 active」），
   而 `search.latest_versions` / `fusion.fold_versions` 此前没有任何测试覆盖。

⚠️ 顺带覆盖 §15 风险 #8：**effective_date 是未来日期**时，
   该文档暂不参与检索、且旧版继续生效（不能出现政策真空期）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest_asyncio

from app import db
from app.retrieval.search import bm25_retrieve, vector_retrieve
from tests.support import (
    delete_admin,
    ingest_text_doc,
    insert_admin,
    make_user,
    purge_documents,
)

# 只出现在本文件合成语料里的短语
MARKER = "量子奶酪"


def _v1_text(tag: str) -> str:
    """旧版：MARKER 反复出现 —— 措辞与 query 高度相似，召回分必然高于新版。"""
    return (
        f"【版本折叠测试 v1 {tag}】\n\n"
        + f"本段反复出现「{MARKER}」，使旧版与 query 的相似度远高于现行版。{MARKER}。\n" * 14
    )


def _v2_text(tag: str) -> str:
    """现行版：MARKER 只出现一次 —— 相似度低，但仍可被召回。"""
    return (
        f"【版本折叠测试 v2 {tag}】\n\n"
        f"现行版本。本段仅出现一次「{MARKER}」，其余为无关正文。\n"
        + "本段是为了凑足分块长度而重复的正文，不含关键词。\n" * 14
    )


@pytest_asyncio.fixture
async def two_versions():
    """同 title 的两份文档：v1 措辞更像 query，v2 是现行版。"""
    admin_id = await insert_admin()
    title = f"版本折叠_{uuid.uuid4().hex[:8]}"

    await ingest_text_doc(admin_id=admin_id, title=title, text=_v1_text(title))
    v2_id = await ingest_text_doc(admin_id=admin_id, title=title, text=_v2_text(title))

    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id, version FROM documents WHERE title = $1 ORDER BY version", title)
    versions = {r["version"]: r["id"] for r in rows}
    assert versions.get(2) == v2_id, f"版本号分配不对：{versions}"

    yield {"title": title, "v1": versions[1], "v2": versions[2]}
    await purge_documents([title])
    await delete_admin(admin_id)


async def _retrieved(user, query: str = MARKER):
    async with db.tx() as conn:
        vec = await vector_retrieve(conn, query, user)
        bm = await bm25_retrieve(conn, query, user)
    return vec, bm


# ---- 折叠：v1 更像 query，但必须返回 v2 ------------------------------------

async def test_old_version_is_dropped_even_though_it_matches_better(two_versions):
    """★ M1-3 核心测试点：回查 PG 生效，而不是在召回集内取最大。"""
    for path_name, chunks in zip(("向量路", "BM25 路"),
                                 await _retrieved(make_user("student"))):
        ids = {c.document_id for c in chunks}
        assert two_versions["v1"] not in ids, (
            f"{path_name}返回了已废止的 v1 —— 折叠没生效（或在召回集内取的 max）"
        )
        assert two_versions["v2"] in ids, (
            f"{path_name}没返回现行版 v2 —— 折叠把候选清空了"
        )


async def test_both_paths_fold_consistently(two_versions):
    """两路都要折叠，且是**折叠**而不是把整组滤掉。

    ⚠️ 断言必须限定在**本版本组内**：两路各自还会带回不相干的语料 chunk
       （它们是 v1，但不是这一组），拿「所有 chunk 的 version」当判据会误伤。
    """
    vec, bm = await _retrieved(make_user("student"))
    assert vec and bm, "两路都应有结果（否则本用例没测到折叠）"
    group = {two_versions["v1"], two_versions["v2"]}

    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        seen = {c.version for c in chunks if c.document_id in group}
        assert seen, f"{path_name}把整个版本组都滤掉了 —— 那是滤没了，不是折叠"
        assert seen == {2}, f"{path_name}在版本组里返回了非现行版：{seen}"


# ---- §15 风险 #8：未来生效日 → 旧版继续生效，不出现政策真空期 ---------------

@pytest_asyncio.fixture
async def future_version():
    """v1 已生效；v2 的 effective_date 在未来（8 月提前传 9 月生效）。"""
    admin_id = await insert_admin()
    title = f"未来生效_{uuid.uuid4().hex[:8]}"

    v1_id = await ingest_text_doc(admin_id=admin_id, title=title, text=_v1_text(title))
    v2_id = await ingest_text_doc(
        admin_id=admin_id, title=title, text=_v2_text(title),
        effective_date=date(2099, 1, 1),
    )

    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id, version, effective_date FROM documents WHERE title = $1 ORDER BY version",
            title)
    assert {r["version"] for r in rows} == {1, 2}

    yield {"title": title, "v1": v1_id, "v2": v2_id}
    await purge_documents([title])
    await delete_admin(admin_id)


async def test_future_effective_version_does_not_take_effect_yet(future_version):
    """★ §15 #8：未来生效的 v2 暂不参与检索，**v1 继续生效**。"""
    for path_name, chunks in zip(("向量路", "BM25 路"),
                                 await _retrieved(make_user("student"))):
        ids = {c.document_id for c in chunks}
        assert future_version["v2"] not in ids, f"{path_name}返回了尚未生效的 v2"
        assert future_version["v1"] in ids, (
            f"{path_name}把 v1 也滤掉了 —— 未来生效期间出现了政策真空期"
        )
