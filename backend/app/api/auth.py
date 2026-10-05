"""认证接口（§3.7.1）。

    POST /api/auth/login     → { access_token, refresh_token, user }
    POST /api/auth/refresh   → { access_token, refresh_token }   ← 用 refresh_token 换新令牌
    POST /api/auth/logout
    GET  /api/auth/me

令牌生命周期与撤销（口径钉死，§3.7.1）：
  - access 15 分钟、refresh 7 天，两者都带当前 tv
  - refresh 校验签名 + 未过期 + tv 匹配；通过后签发**新的 access_token**，
    **refresh_token 原样返回、不轮换**（轮换需要 jti 黑名单，本轮不引入）
  - logout 做 token_version += 1 → 该用户**所有已签发令牌立即失效**
  - 改密/改角色/重置口令同样触发撤销

明确不做：jti 黑名单（能精确撤销单个令牌，但需要新表 + 定期清理 + 每请求查表）。
"""

from __future__ import annotations

import jwt as pyjwt
from fastapi import APIRouter, status

from app import db
from app.core.config import cfg
from app.core.deps import CurrentUser
from app.core.exceptions import Unauthorized
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    TokenPair,
    UserInfo,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _access_ttl_seconds() -> int:
    return int(cfg("auth.access_token_minutes", 15)) * 60


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest) -> TokenPair:
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, password_hash, token_version "
            "  FROM users WHERE username = $1",
            body.username,
        )

    # 用户不存在与口令错误返回同一条消息 —— 不用错误响应泄露用户名是否存在
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise Unauthorized("用户名或口令不正确", code="invalid_credentials")

    tv = row["token_version"]
    return TokenPair(
        access_token=create_access_token(row["id"], row["role"], tv),
        refresh_token=create_refresh_token(row["id"], row["role"], tv),
        expires_in=_access_ttl_seconds(),
        user=UserInfo(id=row["id"], username=row["username"], role=row["role"]),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token)
    except pyjwt.ExpiredSignatureError:
        raise Unauthorized("刷新令牌已过期，请重新登录", code="refresh_expired") from None
    except pyjwt.PyJWTError:
        raise Unauthorized("刷新令牌无效") from None

    if payload.get("type") != TOKEN_TYPE_REFRESH:
        raise Unauthorized("令牌类型不正确")

    user_id = payload.get("sub")
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, token_version FROM users WHERE id = $1", user_id
        )
    if row is None:
        raise Unauthorized("用户不存在")

    # tv 匹配校验：登出后 refresh 也必须失效
    if int(payload.get("tv", -1)) != row["token_version"]:
        raise Unauthorized("刷新令牌已失效，请重新登录", code="token_revoked")

    return TokenPair(
        access_token=create_access_token(row["id"], row["role"], row["token_version"]),
        # 原样返回，不轮换
        refresh_token=body.refresh_token,
        expires_in=_access_ttl_seconds(),
        user=UserInfo(id=row["id"], username=row["username"], role=row["role"]),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(user: CurrentUser) -> MessageResponse:
    """登出 —— token_version += 1，该用户所有设备的所有令牌立即失效。

    粒度是「用户级」，这对校园问答场景可接受（学生一般单设备），
    且比「登出后令牌还能用」安全得多。
    """
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE id = $1", user.id
        )
    return MessageResponse(message="已登出，所有已签发的令牌均已失效")


@router.get("/me", response_model=UserInfo)
async def me(user: CurrentUser) -> UserInfo:
    return UserInfo(id=user.id, username=user.username, role=user.role)
