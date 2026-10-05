"""ACL + 版本过滤条件拼装 —— **唯一入口**（§3.3.3 / §3.6）。

⚠️ 过滤条件只在这里生成，三方都调它：
     ① 向量检索  ② BM25 侧文档粒度过滤  ③ User 端原文访问
        （`/api/documents/{id}/files`、`/text`、`/images/{name}`）

   原文访问必须走它，而不只是检索走它：
   `/api/documents/{id}/file` 是一条绕过检索期 ACL 的捷径 —— 搜不到不等于下不到。
   若那里另写一套判定，两套迟早不一致，亮点① 的「检索期数据隔离」就被这条捷径架空。

过滤公式（§3.3.3）：

    status = "active"                          ← 状态过滤
      AND effective_date <= 今天                ← 生效日期过滤
      AND (visibility = "public"
           OR vis_<角色> = true)                ← ACL（角色布尔字段）

**「同组取最高 version」不在这里表达** —— Chroma 做不到跨条目聚合，
由应用层在检索后完成（§3.3.1 的两段式折叠，见 services/index_service.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from app.core.deps import ROLE_ADMIN, ALL_ROLES, UserContext

VIS_PREFIX = "vis_"


def visibility_field(role: str) -> str:
    """角色 → Chroma 布尔字段名。

    ⚠️ 三个角色必须各有一个字段（§3.3.2）：
       管理端允许「仅勾选学生」，而过滤公式是通用的 OR vis_<角色> = true ——
       缺了 vis_student，这类文档谁也筛不出来，等于建了一份「谁都看不到」的文档，
       且不会报错。角色取值有几个，布尔字段就得有几个。
    """
    if role not in ALL_ROLES:
        raise ValueError(f"未知角色 {role!r}，可选：{ALL_ROLES}")
    return f"{VIS_PREFIX}{role}"


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    # 越权取得：管理端以 include_restricted=true 拿到了本无权限的文档（§3.3.3）
    escalated: bool = False


def resolve_escalation(user: UserContext | str, include_restricted: bool | None) -> bool:
    """提权只对 admin 生效。

    ⚠️ 非 admin 传了按未传处理 —— **不报错**。
       与 chat 接口同一口径：不要用错误响应泄露角色差异。
    """
    role = user.role if isinstance(user, UserContext) else user
    return bool(include_restricted) and role == ROLE_ADMIN


def date_key(value: date) -> int:
    """日期 → 整数 YYYYMMDD，供 Chroma 比较。

    ⚠️⚠️ **Chroma 的 `$lte` / `$gte` 只接受 int / float，不接受字符串** ——
       传 ISO 日期串会直接抛
       `Expected operand value to be an int or a float for operator $lte, got 2026-10-05`，
       且**只在运行时暴露**。
       2026-10-05 实测踩过：向量路 12 次请求**全部降级**，
       全程静默退回 BM25 单路（降级链兜住了，表面看只是"召回少一点"，
       极易被当成正常波动）——**混合检索等于从来没生效过**。

       因此 Chroma metadata 里的 `effective_date` 存 **int（YYYYMMDD）**；
       PostgreSQL `documents.effective_date` 仍是 DATE，是唯一权威来源。
    """
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return int(value.strftime("%Y%m%d"))


def build_where(
    user: UserContext | str,
    *,
    today: date | None = None,
    include_restricted: bool | None = False,
) -> dict[str, Any]:
    """拼装 Chroma 的 `where` 过滤条件。

    返回的字典可直接传给 Chroma 的 `collection.query(where=...)`。

    ⚠️ `include_restricted` 只对 admin 生效；非 admin 传 True 会被当成 False。
    """
    role = user.role if isinstance(user, UserContext) else user
    day = date_key(today or date.today())

    conditions: list[dict[str, Any]] = [
        {"status": {"$eq": "active"}},
        # 整数比较 —— 见 date_key 的说明，字符串会被 Chroma 拒绝
        {"effective_date": {"$lte": day}},
    ]

    # admin 默认走同一公式，需要查看未授权文档时走显式提权（§3.3.3）。
    # 公式对 admin 一视同仁 —— 是否可见只由 visibility 与 vis_<角色> 决定。
    if not resolve_escalation(role, include_restricted):
        conditions.append({
            "$or": [
                {"visibility": {"$eq": "public"}},
                {visibility_field(role): {"$eq": True}},
            ]
        })

    # Chroma 对只有一个操作数的 $and 可能不认，简化掉
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def build_where_bm25(
    user: UserContext | str,
    *,
    today: date | None = None,
    include_restricted: bool | None = False,
) -> dict[str, Any]:
    """BM25 侧用的过滤条件。

    BM25S 没有 metadata，它返回的是自身语料库里的**下标**。因此过滤时靠旁挂的
    「下标 → chunk_id」映射表查出 chunk，再由 chunk 追到文档，回 `documents` 表
    判 status / effective_date / 可见性（§3.6）。

    本函数产出与 `build_where` 同构的条件，供调用方在**文档粒度**上套用，
    避免两路过滤规则不一致。
    """
    return build_where(user, today=today, include_restricted=include_restricted)


def allowed_document_ids(
    where: dict[str, Any], docs: list[dict[str, Any]], *, today: date | None = None
) -> set[str]:
    """把 Chroma 风格的 where 条件套用到一批文档行上（纯 Python 判定）。

    BM25 侧回查 `documents` 表拿到行之后，用这个函数做与向量路**完全同构**的判定，
    避免「向量路过滤了、BM25 路没过滤」这种最危险的偏差。

    `docs` 每项需含：id / status / effective_date / visibility / visible_roles
    """
    day = (today or date.today())
    out: set[str] = set()

    acl = _extract_acl(where)
    for doc in docs:
        if doc.get("status") != "active":
            continue
        eff = doc.get("effective_date")
        if eff is None:
            continue
        # PG 返回 date 对象；比较前统一
        eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff)[:10])
        if eff_date > day:
            continue

        if acl is None:
            # 提权模式：跳过 ACL 判定
            out.add(doc["id"])
            continue

        public, roles = acl
        if public and doc.get("visibility") == "public":
            out.add(doc["id"])
            continue
        visible = doc.get("visible_roles")
        if isinstance(visible, str):
            import json
            try:
                visible = json.loads(visible)
            except (ValueError, TypeError):
                visible = []
        if any(r in (visible or []) for r in roles):
            out.add(doc["id"])

    return out


def _extract_acl(where: dict[str, Any]) -> tuple[bool, set[str]] | None:
    """从 where 里解出 ACL 部分，返回 (是否含 public 分支, 允许的角色集合)。

    返回 None 表示该 where 没有 ACL 分支（即提权模式）。
    """
    conditions = where.get("$and", [where])
    for cond in conditions:
        if "$or" in cond:
            public = False
            roles: set[str] = set()
            for branch in cond["$or"]:
                if branch.get("visibility", {}).get("$eq") == "public":
                    public = True
                for key, value in branch.items():
                    if key.startswith(VIS_PREFIX) and value.get("$eq") is True:
                        roles.add(key[len(VIS_PREFIX):])
            return public, roles
    return None


async def can_access(
    conn: asyncpg.Connection,
    document_id: str,
    user: UserContext | str,
    *,
    include_restricted: bool | None = False,
    today: date | None = None,
) -> AccessDecision:
    """单个文档的可见性判定 —— 检索与**原文访问**共用这一个函数（§3.6）。

    调用方据 AccessDecision 决定返回什么（§3.7.2）：
        allowed → 200
        不可见 → **404（不是 403）** —— 让「不可见」与「不存在」不可区分，
                 否则可以被用来探测文档是否存在
    """
    role = user.role if isinstance(user, UserContext) else user

    row = await conn.fetchrow(
        "SELECT id, status, effective_date, visibility, visible_roles "
        "  FROM documents WHERE id = $1",
        document_id,
    )
    if row is None:
        return AccessDecision(allowed=False)

    # 与检索期同构：非 active / 未生效 的文档同样不可访问
    if row["status"] != "active":
        return AccessDecision(allowed=False)

    day = today or date.today()
    if row["effective_date"] and row["effective_date"] > day:
        return AccessDecision(allowed=False)

    if row["visibility"] == "public":
        return AccessDecision(allowed=True)

    visible = row["visible_roles"]
    if isinstance(visible, str):
        import json
        try:
            visible = json.loads(visible)
        except (ValueError, TypeError):
            visible = []

    if role in (visible or []):
        return AccessDecision(allowed=True)

    # 文档没勾当前角色，而管理员显式提权 —— 允许，但标记越权（§3.3.3）
    if resolve_escalation(role, include_restricted):
        return AccessDecision(allowed=True, escalated=True)

    return AccessDecision(allowed=False)
