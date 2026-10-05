"""JWT 签发/校验、密码哈希（§3.2.2 / §3.7.1）。

令牌撤销机制（§3.7.1）：
  JWT 载荷带 tv 声明，解码后与 users.token_version 比对，不等即 401。
  粒度是「用户级」—— 登出会让该用户所有设备的令牌失效。
  代价是每次请求多一次 users 表主键查询；表小、索引命中，开销可忽略。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import cfg, secret

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


# ---- 密码 --------------------------------------------------------------

def hash_password(plain: str) -> str:
    """bcrypt 哈希。bcrypt 只取前 72 字节，超长口令先截断避免静默失效。"""
    raw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---- JWT ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(
        payload,
        secret(cfg("auth.jwt_secret_env", "JWT_SECRET")),
        algorithm=cfg("auth.jwt_algorithm", "HS256"),
    )


def create_access_token(user_id: str, role: str, token_version: int) -> str:
    """access_token：15 分钟（§3.7.1）。"""
    minutes = int(cfg("auth.access_token_minutes", 15))
    return _encode({
        "sub": user_id,
        "role": role,
        "tv": token_version,
        "type": TOKEN_TYPE_ACCESS,
        "iat": _now(),
        "exp": _now() + timedelta(minutes=minutes),
        "jti": uuid.uuid4().hex,
    })


def create_refresh_token(user_id: str, role: str, token_version: int) -> str:
    """refresh_token：7 天（§3.7.1）。不轮换 —— 刷新时原样返回。"""
    days = int(cfg("auth.refresh_token_days", 7))
    return _encode({
        "sub": user_id,
        "role": role,
        "tv": token_version,
        "type": TOKEN_TYPE_REFRESH,
        "iat": _now(),
        "exp": _now() + timedelta(days=days),
        "jti": uuid.uuid4().hex,
    })


def decode_token(token: str) -> dict[str, Any]:
    """解码并校验签名与有效期。失败抛 jwt.PyJWTError 的子类。

    注意：这里**不查库**比对 token_version —— 那是 deps.current_user 的职责，
    因为它需要数据库连接。
    """
    return jwt.decode(
        token,
        secret(cfg("auth.jwt_secret_env", "JWT_SECRET")),
        algorithms=[cfg("auth.jwt_algorithm", "HS256")],
    )
