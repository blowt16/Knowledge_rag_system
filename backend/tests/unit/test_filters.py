"""过滤条件拼装（§3.3.3）—— 亮点① 的地基。

对应施工计划 M0-5 的测试点：
  ① student 的 where 里是 vis_student
  ② public 与角色布尔是 $or
  ③ admin 默认**不**旁路
  ④ 非 admin 传 include_restricted=True 与 False 结果相同
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.deps import UserContext
from app.retrieval import filters
from app.retrieval.filters import build_where, visibility_field


def _user(role: str) -> UserContext:
    return UserContext(id="u1", username="x", role=role, token_version=0)


TODAY = date(2026, 10, 5)


# ---- 基本结构 ----------------------------------------------------------

def test_where_has_status_and_effective_date():
    """⚠️ effective_date 必须是**整数** YYYYMMDD。

    Chroma 的 `$lte` 只接受 int/float —— 传 ISO 字符串会抛
    `Expected operand value to be an int or a float ... got 2026-10-05`，
    且只在运行时暴露。2026-10-05 实测：向量路因此**全部降级**、
    静默退回 BM25 单路，混合检索从没生效过。本断言锁死这个回归。
    """
    where = build_where("student", today=TODAY)
    conditions = where["$and"]
    assert {"status": {"$eq": "active"}} in conditions
    assert {"effective_date": {"$lte": 20261005}} in conditions
    # 显式排除字符串形式
    for cond in conditions:
        if "effective_date" in cond:
            assert not isinstance(cond["effective_date"]["$lte"], str)


def test_date_key_shape():
    from datetime import date as _d

    from app.retrieval.filters import date_key

    assert date_key(_d(2026, 10, 5)) == 20261005
    assert date_key(_d(2026, 1, 1)) == 20260101
    assert date_key("2026-10-05") == 20261005


@pytest.mark.parametrize("role,field", [
    ("student", "vis_student"),
    ("staff", "vis_staff"),
    ("admin", "vis_admin"),
])
def test_role_specific_visibility_field(role, field):
    """★ 测试点①：每个角色读自己那个布尔字段。

    三个角色必须各有一个字段 —— 缺了 vis_student，「仅勾选学生」的文档
    谁也筛不出来，且不会报错。
    """
    where = build_where(role, today=TODAY)
    acl = next(c for c in where["$and"] if "$or" in c)
    assert {"visibility": {"$eq": "public"}} in acl["$or"]
    assert {field: {"$eq": True}} in acl["$or"]


def test_visibility_field_rejects_unknown_role():
    with pytest.raises(ValueError):
        visibility_field("teacher")


# ---- 测试点②：public 与角色布尔是 $or，不是 $and -----------------------

def test_public_and_role_are_ored():
    """public 文档与勾选了本角色的文档**都**应可见 —— 用 $and 就全筛不出来。"""
    where = build_where("student", today=TODAY)
    acl = next(c for c in where["$and"] if "$or" in c)
    assert len(acl["$or"]) == 2


# ---- 测试点③④：admin 默认不旁路 ---------------------------------------

def test_admin_defaults_to_same_formula_not_bypass():
    """★ admin 默认走同一公式 —— 不是天然看到一切。

    这是 5.3 ACL 对照实验基准可信的前提：admin 能跑全部题必须是
    「因为显式提权」，而不是「因为有旁路」。
    """
    where = build_where("admin", today=TODAY)
    acl = next(c for c in where["$and"] if "$or" in c)
    assert {"vis_admin": {"$eq": True}} in acl["$or"]


def test_admin_escalation_drops_acl_branch():
    where = build_where("admin", today=TODAY, include_restricted=True)
    assert not any("$or" in c for c in where["$and"]), "提权后不应再有 ACL 分支"


@pytest.mark.parametrize("role", ["student", "staff"])
def test_non_admin_escalation_ignored_silently(role):
    """★ 测试点④：非 admin 传了按未传处理 —— **不报错**。

    用错误响应拒绝等于告诉对方「这个参数对你有特殊含义」，
    可以被用来探测角色差异（§3.7.2）。
    """
    assert build_where(role, today=TODAY, include_restricted=True) == \
           build_where(role, today=TODAY, include_restricted=False)


def test_resolve_escalation_only_admin():
    assert filters.resolve_escalation("admin", True) is True
    assert filters.resolve_escalation("admin", False) is False
    assert filters.resolve_escalation("student", True) is False
    assert filters.resolve_escalation("staff", True) is False


# ---- BM25 侧同构判定 ---------------------------------------------------

def test_allowed_document_ids_matches_where_semantics():
    """BM25 侧回查 PG 后的判定必须与向量路同构。

    最危险的偏差是「向量路过滤了、BM25 路没过滤」——
    那样旧版/受限文档会从 BM25 那一路漏出来。
    """
    where = build_where("student", today=TODAY)
    docs = [
        {"id": "public_ok", "status": "active", "effective_date": date(2025, 1, 1),
         "visibility": "public", "visible_roles": None},
        {"id": "student_ok", "status": "active", "effective_date": date(2025, 1, 1),
         "visibility": "restricted", "visible_roles": ["student"]},
        {"id": "admin_only", "status": "active", "effective_date": date(2025, 1, 1),
         "visibility": "restricted", "visible_roles": ["admin"]},
        {"id": "future", "status": "active", "effective_date": date(2099, 1, 1),
         "visibility": "public", "visible_roles": None},
        {"id": "disabled", "status": "disabled", "effective_date": date(2025, 1, 1),
         "visibility": "public", "visible_roles": None},
        {"id": "indexing", "status": "indexing", "effective_date": date(2025, 1, 1),
         "visibility": "public", "visible_roles": None},
    ]
    allowed = filters.allowed_document_ids(where, docs, today=TODAY)
    assert allowed == {"public_ok", "student_ok"}


def test_allowed_document_ids_handles_json_string_roles():
    """visible_roles 从 asyncpg 出来可能是 JSON 字符串，也得能解析。"""
    where = build_where("staff", today=TODAY)
    docs = [{"id": "d1", "status": "active", "effective_date": date(2025, 1, 1),
             "visibility": "restricted", "visible_roles": '["staff", "admin"]'}]
    assert filters.allowed_document_ids(where, docs, today=TODAY) == {"d1"}


def test_escalation_where_allows_everything_active():
    where = build_where("admin", today=TODAY, include_restricted=True)
    docs = [
        {"id": "admin_only", "status": "active", "effective_date": date(2025, 1, 1),
         "visibility": "restricted", "visible_roles": ["staff"]},
        {"id": "disabled", "status": "disabled", "effective_date": date(2025, 1, 1),
         "visibility": "public", "visible_roles": None},
    ]
    # 提权放开的是 ACL，**不是** status/生效日 —— disabled 仍然排除
    assert filters.allowed_document_ids(where, docs, today=TODAY) == {"admin_only"}
