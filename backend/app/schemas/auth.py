"""认证相关契约（§3.7.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserInfo(BaseModel):
    id: str
    username: str
    role: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    # access_token 有效期（秒）—— 前端据此决定何时静默续期
    expires_in: int
    user: UserInfo | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    message: str
