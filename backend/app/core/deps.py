"""依赖注入：current_user / require_role（§3.2.2）。

**关键约束**：角色与身份**只能来自 JWT**，任何接口都不得接受客户端传入的 user_id。
这是现有项目最大的安全缺陷（附录 A 第 2 条：现在传谁的 id 就能读谁的资料）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import db
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import TOKEN_TYPE_ACCESS, decode_token

_bearer = HTTPBearer(auto_error=False)

ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
ROLE_STUDENT = "student"
ALL_ROLES = (ROLE_ADMIN, ROLE_STAFF, ROLE_STUDENT)


@dataclass(frozen=True)
class UserContext:
    id: str
    username: str
    role: str
    token_version: int


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UserContext:
    if credentials is None or not credentials.credentials:
        raise Unauthorized("缺少访问令牌")

    try:
        payload = decode_token(credentials.credentials)
    except pyjwt.ExpiredSignatureError:
        raise Unauthorized("访问令牌已过期", code="token_expired") from None
    except pyjwt.PyJWTError:
        raise Unauthorized("访问令牌无效") from None

    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise Unauthorized("令牌类型不正确")

    user_id = payload.get("sub")
    if not user_id:
        raise Unauthorized("令牌缺少主体")

    # token_version 比对（§3.7.1）—— 每次请求一次查库，users 表小、主键索引命中
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, token_version FROM users WHERE id = $1", user_id
        )
    if row is None:
        raise Unauthorized("用户不存在")

    if int(payload.get("tv", -1)) != row["token_version"]:
        # 登出 / 改密 / 改角色 / 重置口令都会自增 token_version
        raise Unauthorized("令牌已失效，请重新登录", code="token_revoked")

    return UserContext(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        token_version=row["token_version"],
    )


CurrentUser = Annotated[UserContext, Depends(current_user)]


def require_role(*roles: str):
    """路由守卫：只允许指定角色访问。"""

    async def _guard(user: CurrentUser) -> UserContext:
        if user.role not in roles:
            raise Forbidden("权限不足")
        return user

    return _guard
