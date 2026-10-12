"""用户与角色（§4.3 用户管理 / §3.7.1）。

⚠️ **三处写操作都要令 `token_version += 1`**（§3.7.3 的接口清单末尾）：
   建号 / 改角色 / 重置口令。自增即让该用户**所有已签发令牌立即失效** ——
   否则「把某人从 admin 降成 student，他还能拿着旧令牌继续当 admin」。

⚠️ 但 `delete_user` **是例外，不自增**（行都要没了，自增无处可增）——
   别照抄上面那条规矩往上套，理由见函数内的说明。
"""

from __future__ import annotations

import uuid

import asyncpg

from app import db
from app.core.deps import ALL_ROLES
from app.core.exceptions import AppError, Conflict, NotFound
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


async def delete_user(user_id: str, *, actor_id: str) -> None:
    """删除用户（§4.3 用户管理）—— **真删**，连带清掉他的会话与消息。

    ⚠️ **本模块唯一不自增 `token_version` 的写操作**：行都没了，自增无处可增。
       令牌靠 `deps.current_user` 查库查不到来作废，不需要（也无法）自增。

    ⚠️ **顺序不能动**：`messages.conversation_id` 是 NOT NULL 外键，必须先清
       消息再清会话。整段在同一个事务里 —— 顺序反了外键会报错，并把这一批
       删除**全部回滚**，报错点还离真正的错因很远。删号本身也必须与这些清理
       同事务，否则半个用户删掉就等于留下一堆指向空气的外键。

    ⚠️ **不许删自己**。除了防手滑，它还顺带保证「管理员删不光」：能发出这个
       请求的人一定是当下活着、且持有有效令牌的管理员（令牌有效 ⇒ 登录时账号
       是启用的），他删得掉别人、删不掉自己，所以系统里永远至少剩一个能用的
       管理员。因此**不需要**再写一条「不许删最后一个管理员」的特判。

    留下什么：文档、评测集、qa_logs 都保留，只把指向本人的归属列置空 ——
    共享语料与评测数据不该随人走（`documents.uploader_id` 那几列本就是可空的），
    而 qa_logs 无外键、按设计只增不删，是管理端拒答分析的数据源。
    """
    if user_id == actor_id:
        raise AppError("不能删除自己的账号")

    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
        if row is None:
            raise NotFound("用户不存在")

        await conn.execute(
            """DELETE FROM messages WHERE conversation_id IN
                   (SELECT id FROM conversations WHERE user_id = $1)""", user_id)
        await conn.execute("DELETE FROM conversations WHERE user_id = $1", user_id)

        await conn.execute(
            "UPDATE documents SET uploader_id = NULL WHERE uploader_id = $1", user_id)
        await conn.execute(
            "UPDATE ingestion_tasks SET uploader_id = NULL WHERE uploader_id = $1",
            user_id)
        await conn.execute(
            "UPDATE eval_sets SET created_by = NULL WHERE created_by = $1", user_id)
        await conn.execute(
            "UPDATE refusal_annotations SET annotated_by = NULL WHERE annotated_by = $1",
            user_id)

        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


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
