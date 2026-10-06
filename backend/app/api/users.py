"""管理端：用户与角色（§3.7.3 / §4.3 用户管理）。

**分层**（A7）：这里只做参数校验与响应封装，业务在 `services/user_service.py`。

⚠️ 守卫写法见 `api/admin.py` 顶部的说明（裸 `Depends(...)` 要当**默认值**用）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_role
from app.core.exceptions import AppError
from app.schemas.user import (
    CreateUserRequest,
    PatchUserRequest,
    ResetPasswordRequest,
    RoleItem,
    UserItem,
    UserListResponse,
)
from app.services import user_service as users

router = APIRouter(prefix="/api/admin", tags=["admin:users"])

AdminUser = Depends(require_role("admin"))


@router.get("/roles", response_model=list[RoleItem])
async def list_roles(user=AdminUser) -> list[RoleItem]:
    """角色清单 —— 可见范围选择器要用，**不许前端硬编码**（§4.3.1.2）。"""
    return [RoleItem(**r) for r in users.role_options()]


@router.get("/users", response_model=UserListResponse)
async def list_users(
    user=AdminUser,
    role: str | None = Query(None, pattern="^(student|staff|admin)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> UserListResponse:
    return UserListResponse(**await users.list_users(
        role=role, page=page, page_size=page_size))


@router.post("/users", response_model=UserItem)
async def create_user(body: CreateUserRequest, user=AdminUser) -> UserItem:
    return UserItem(**await users.create_user(
        username=body.username, password=body.password, role=body.role))


@router.patch("/users/{user_id}", response_model=UserItem)
async def patch_user(user_id: str, body: PatchUserRequest, user=AdminUser) -> UserItem:
    if body.role is None and body.is_active is None:
        raise AppError("至少要给出 role 或 is_active 之一")
    return UserItem(**await users.patch_user(
        user_id, role=body.role, is_active=body.is_active))


@router.post("/users/{user_id}/reset-password", response_model=UserItem)
async def reset_password(user_id: str, body: ResetPasswordRequest,
                         user=AdminUser) -> UserItem:
    return UserItem(**await users.reset_password(user_id, password=body.password))
