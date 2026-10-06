"""管理端用户与角色的契约（§3.7.3 / §4.3 用户管理）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RoleValue = Literal["student", "staff", "admin"]


class RoleItem(BaseModel):
    """`GET /api/admin/roles` → `[{value, label}]`（§4.3.1.2）。"""

    value: str
    label: str


class UserItem(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool = True
    created_at: str | None = None


class UserListResponse(BaseModel):
    items: list[UserItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)
    role: RoleValue


class PatchUserRequest(BaseModel):
    """改角色 / 停用。两个字段都可选，**至少给一个**（空体由路由判 400）。"""

    model_config = ConfigDict(extra="forbid")

    role: RoleValue | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=8, max_length=128)
