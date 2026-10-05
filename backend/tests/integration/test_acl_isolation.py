"""检索期 ACL 隔离（§3.3.3 / §3.6）—— M1 验收项②。

验收原文：**student 搜不到 `vis_admin` 的文档**。

⚠️ 必须**两路都测**：向量路靠 Chroma 的 `where` 过滤，BM25 路靠回查 PG 做同构判定。
   只测一路的话，另一路漏掉不会被发现 —— 而「向量路过滤了、BM25 路没过滤」
   正是 §3.6 点名的、这类偏差里最危险的一种。

⚠️ 用例的正文是**合成的唯一短语**，query 直接用那个短语。
   这样「过滤失效」必然表现为「该 chunk 排在最前面」，
   不会出现「反正也排不进 Top-K，测了个寂寞」的假通过；
   并配一条**正向对照**（有权限时确实搜得到）来证明本文件不是空转。

⚠️ 清理必须同时清 Chroma 与 BM25S —— 只清一处会往真实索引里漏死条目。
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest_asyncio

from app import db
from app.core.deps import UserContext
from app.retrieval.filters import can_access
from app.retrieval.search import bm25_retrieve, vector_retrieve
from tests.support import (
    delete_admin,
    ingest_text_doc,
    insert_admin,
    make_user,
    purge_documents,
)

MARKER = "量子奶酪"


def _body(tag: str) -> str:
    return (
        f"【ACL 隔离测试专用文档 {tag}】\n\n"
        f"本文用于验证检索期行级权限过滤，包含唯一关键词「{MARKER}」。\n"
        f"任何无权用户检索到含「{MARKER}」的内容，都说明行级 ACL 失效。\n"
        f"再次出现关键词「{MARKER}」以便分块后仍然集中。\n"
        + "本段是为了凑足分块长度而重复的正文，不含其他关键词。\n" * 12
    )


def _user(role: str) -> UserContext:
    return make_user(role)


async def _ingest_restricted(admin_id: str, roles: list[str]) -> tuple[str, str]:
    """造一份受限文档，返回 (title, document_id)。"""
    title = f"ACL测试_{'_'.join(roles) or 'none'}_{uuid.uuid4().hex[:8]}"
    doc_id = await ingest_text_doc(
        admin_id=admin_id, title=title, text=_body(title),
        visibility="restricted", visible_roles=roles,
    )
    return title, doc_id


@pytest_asyncio.fixture
async def admin_user_id() -> str:
    user_id = await insert_admin()
    yield user_id
    await delete_admin(user_id)


@pytest_asyncio.fixture
async def restricted_docs(admin_user_id: str):
    """两份受限文档：一份只给 admin 看、一份只给 staff 看。"""
    titles = []
    t1, admin_only = await _ingest_restricted(admin_user_id, ["admin"])
    t2, staff_only = await _ingest_restricted(admin_user_id, ["staff"])
    titles += [t1, t2]
    yield {"admin_only": admin_only, "staff_only": staff_only}
    await purge_documents(titles)


async def _retrieved(user: UserContext, *, include_restricted: bool = False):
    """跑两路，返回 (向量路 chunk 列表, BM25 路 chunk 列表)。"""
    async with db.tx() as conn:
        vec = await vector_retrieve(conn, MARKER, user,
                                    include_restricted=include_restricted)
        bm = await bm25_retrieve(conn, MARKER, user,
                                 include_restricted=include_restricted)
    return vec, bm


# ---- student：两路都搜不到 -------------------------------------------------

async def test_student_retrieves_neither_restricted_document(restricted_docs):
    """★ M1 验收项②。"""
    vec, bm = await _retrieved(_user("student"))

    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        ids = {c.document_id for c in chunks}
        assert restricted_docs["admin_only"] not in ids, f"{path_name}漏了 admin 专属文档"
        assert restricted_docs["staff_only"] not in ids, f"{path_name}漏了 staff 专属文档"


# ---- 正向对照：有权限时确实搜得到 -----------------------------------------

async def test_staff_retrieves_own_but_not_admin_only(restricted_docs):
    """★ 反空转：staff 有权限时**必须搜得到**，否则上面的「搜不到」可能只是没命中。

    同一个 MARKER 短语在两份文档里都有，所以两路都必然命中 ——
    只有 ACL 能造成「搜不到」。
    """
    vec, bm = await _retrieved(_user("staff"))

    assert restricted_docs["staff_only"] in {c.document_id for c in vec}, "向量路没搜到 staff 文档"
    assert restricted_docs["staff_only"] in {c.document_id for c in bm}, "BM25 路没搜到 staff 文档"

    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        ids = {c.document_id for c in chunks}
        assert restricted_docs["admin_only"] not in ids, f"{path_name}漏了 admin 专属文档"


async def test_admin_without_escalation_sees_only_its_own(restricted_docs):
    """admin 默认**不旁路** —— 它是靠 vis_admin 正常可见，不是靠角色特权。"""
    vec, bm = await _retrieved(_user("admin"))

    assert restricted_docs["admin_only"] in {c.document_id for c in vec}
    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        ids = {c.document_id for c in chunks}
        assert restricted_docs["staff_only"] not in ids, (
            f"{path_name}在未提权时拿到了 staff 专属文档 —— admin 变成了旁路"
        )


# ---- 提权：拿得到，且必须标出来 -------------------------------------------

async def test_escalation_marks_chunks_on_both_paths(restricted_docs):
    """★ 提权取得的 chunk 必须在**两路**都标 `escalated=True`。

    `Citation.escalated` 是 5.3 ACL 对照实验直接读的字段（§3.3.3）。
    两路标法不一致的话，「哪些引用是越权拿的」就有一半查不出来。
    """
    vec, bm = await _retrieved(_user("admin"), include_restricted=True)
    target = restricted_docs["staff_only"]

    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        hit = [c for c in chunks if c.document_id == target]
        assert hit, f"{path_name}提权后仍未拿到 staff 专属文档"
        assert all(c.escalated for c in hit), (
            f"{path_name}提权取得的 chunk 没标 escalated —— 越权引用会被当成正常引用"
        )


async def test_escalation_is_ignored_for_non_admin(restricted_docs):
    """非 admin 传 include_restricted 按未传处理 —— **不报错**（与 chat 接口同口径）。"""
    vec, bm = await _retrieved(_user("student"), include_restricted=True)

    for path_name, chunks in (("向量路", vec), ("BM25 路", bm)):
        ids = {c.document_id for c in chunks}
        assert restricted_docs["admin_only"] not in ids
        assert restricted_docs["staff_only"] not in ids


# ---- 原文访问共用同一套判定（§3.6）----------------------------------------

async def test_can_access_matches_retrieval_verdict(restricted_docs):
    """`can_access` 是检索与**原文访问**共用的那一个函数。

    它必须与检索期判定一致 —— 否则 `/api/documents/{id}/file` 就是绕过 ACL 的捷径
    （搜不到 ≠ 下不到）。
    """
    async with db.tx() as conn:
        student_on_admin = await can_access(conn, restricted_docs["admin_only"], _user("student"))
        admin_on_staff = await can_access(conn, restricted_docs["staff_only"], _user("admin"))
        staff_on_own = await can_access(conn, restricted_docs["staff_only"], _user("staff"))
        admin_escalated = await can_access(conn, restricted_docs["staff_only"], _user("admin"),
                                           include_restricted=True)

    assert student_on_admin.allowed is False
    assert student_on_admin.escalated is False
    assert admin_on_staff.allowed is False, "admin 不提权不该拿到 staff 专属文档"
    assert staff_on_own.allowed is True
    assert admin_escalated.allowed is True and admin_escalated.escalated is True
