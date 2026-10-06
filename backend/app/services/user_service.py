"""用户与角色（§4.3 用户管理 / §3.7.1）。

⚠️ **三处写操作都要令 `token_version += 1`**（§3.7.3 的接口清单末尾）：
   建号 / 改角色 / 重置口令。自增即让该用户**所有已签发令牌立即失效** ——
   否则「把某人从 admin 降成 student，他还能拿着旧令牌继续当 admin」。
"""

from __future__ import annotations

import uuid

import asyncpg

from app import db
from app.core.deps import ALL_ROLES
from app.core.exceptions import Conflict, NotFound
from app.core.security import hash_password

# 角色清单由服务端给出 —— §4.3.1.2 明令**不许前端硬编码**，
# 也不让前端去读 `users.role` 的取值集合
ROLE_LABELS = {"admin": "管理员", "staff": "教职工", "student": "学生"}


def role_options() -> list[dict]:
    return [{"value": r, "label": ROLE_LABELS.get(r, r)} for r in ALL_ROLES]


def _item(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def _bump_token_version(conn: asyncpg.Connection, user_id: str) -> None:
    """撤销该用户所有已签发令牌（§3.7.1）。"""
    await conn.execute(
        "UPDATE users SET token_version = token_version + 1 WHERE id = $1", user_id)


async def list_users(*, role: str | None = None, page: int = 1,
                     page_size: int = 20) -> dict:
    where, args = "TRUE", []
    if role:
        args.append(role)
        where = "role = $1"
    async with db.tx() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM users WHERE {where}", *args)
        rows = await conn.fetch(
            f"""SELECT id, username, role, is_active, created_at FROM users
                 WHERE {where}
                 ORDER BY created_at DESC
                 OFFSET ${len(args) + 1} LIMIT ${len(args) + 2}""",
            *args, (page - 1) * page_size, page_size)
    return {
        "items": [_item(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    }


async def create_user(*, username: str, password: str, role: str) -> dict:
    """建号（§3.7.3）。首个管理员仍由 CLI 种子脚本创建，本接口管日常账号。"""
    user_id = uuid.uuid4().hex
    try:
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO users (id, username, password_hash, role, token_version)
                   VALUES ($1, $2, $3, $4, 0)""",
                user_id, username, hash_password(password), role,
            )
            # 照 §3.7.3「三处写操作都要 token_version += 1」。
            # 新号此刻没有任何已签发令牌，这次自增是空操作 —— 保持三处写法一致，
            # 免得日后有人照抄另外两处时漏掉。净结果 token_version = 1。
            await _bump_token_version(conn, user_id)
            row = await conn.fetchrow(
                "SELECT id, username, role, is_active, created_at FROM users WHERE id = $1",
                user_id)
    except asyncpg.UniqueViolationError as e:
        raise Conflict("用户名已存在") from e
    return _item(row)


async def patch_user(user_id: str, *, role: str | None = None,
                     is_active: bool | None = None) -> dict:
    """改角色 / 停用（§4.3 用户管理）。"""
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
        if row is None:
            raise NotFound("用户不存在")

        if role is not None:
            await conn.execute("UPDATE users SET role = $2 WHERE id = $1", user_id, role)
        if is_active is not None:
            await conn.execute("UPDATE users SET is_active = $2 WHERE id = $1",
                               user_id, is_active)
        await _bump_token_version(conn, user_id)

        updated = await conn.fetchrow(
            "SELECT id, username, role, is_active, created_at FROM users WHERE id = $1",
            user_id)
    return _item(updated)


async def reset_password(user_id: str, *, password: str) -> dict:
    """重置口令（§3.7.3）—— 同样撤销该用户全部令牌（§3.7.1）。"""
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
        if row is None:
            raise NotFound("用户不存在")

        await conn.execute("UPDATE users SET password_hash = $2 WHERE id = $1",
                           user_id, hash_password(password))
        await _bump_token_version(conn, user_id)

        updated = await conn.fetchrow(
            "SELECT id, username, role, is_active, created_at FROM users WHERE id = $1",
            user_id)
    return _item(updated)
