"""PostgreSQL 驱动与连接管理（§3.3.5 / §3.2.4）。

选型口径（§3.3.5，防止实现者各选一套）：
  - 驱动 asyncpg（原生异步；同步驱动会阻塞事件循环，SSE 流式期间尤其明显）
  - 不用 ORM，SQL 手写
  - 连接池随应用生命周期创建与关闭（lifespan）
  - 暂不引入 Alembic，用 migrations/*.sql 手工推进

两层会话锁（§3.2.4）：
  - 短锁：事务级 advisory lock，保护毫秒级写操作，提交/回滚即自动释放
  - 长锁：session_locks 表 + TTL，保护整个 LLM 生成过程
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from app.core.config import cfg, pg_dsn

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """在应用 lifespan 启动时创建连接池。"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=pg_dsn(), min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("连接池尚未初始化 —— 请在 lifespan 中调用 init_pool()")
    return _pool


@asynccontextmanager
async def tx() -> AsyncIterator[asyncpg.Connection]:
    """一个连接 + 一个事务。

    ⚠️ 需要「加锁 → 写 → 提交」原子性的场景**必须**用这个助手，
    不要自己写两次独立的 conn.execute —— 见 short_lock 的说明。
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


# ============================================================
# 短锁：事务级 advisory lock（§3.2.4 / §3.3.5）
# ============================================================

async def short_lock(conn: asyncpg.Connection, session_id: str) -> None:
    """在**当前事务**内获取会话短锁，保护消息追加、状态更新等毫秒级写操作。

    ⚠️⚠️ 这是本设计最容易踩的坑（§3.3.5）：
        pg_advisory_xact_lock 只在当前事务内持有，事务提交即释放。
        因此「加锁 → 写 → 提交」必须在**同一个事务**里：

            async with tx() as conn:                 # ← 事务开始
                await short_lock(conn, session_id)   # ← 加锁
                await conn.execute(...)              # ← 写在锁保护下
            # 事务提交，锁自动释放

        若写成两次独立的 conn.execute（各自自动提交），
        **锁在真正写之前就已经释放了** —— 看着有锁，实际等于没加。
        这个坑的特征是：不报错，只是偶尔并发跑两份。
    """
    # hashtext 返回 int4，可隐式转 bigint，无需显式 cast（附录 G 实测）
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('session:' || $1))", session_id
    )


# ============================================================
# 长锁：session_locks 表 + TTL（§3.2.4）
# ============================================================

async def acquire_long_lock(session_id: str, holder: str) -> bool:
    """申请会话长锁。返回 True 表示拿到，False 表示被他人持有且未过期。

    SQL 原子完成「空闲则拿、过期则抢」。

    ⚠️ WHERE session_locks.expires_at < now() 必须有：
        没有它，ON CONFLICT DO UPDATE 会无条件抢占，变成「后到者赢」——
        同一会话的两个并发请求会同时开始生成，锁等于没加。
    """
    ttl = int(cfg("session.lock_ttl_seconds", 120))
    async with tx() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO session_locks
                (session_id, holder, acquired_at, expires_at, heartbeat_at)
            VALUES ($1, $2, now(), now() + make_interval(secs => $3), now())
            ON CONFLICT (session_id) DO UPDATE
               SET holder       = EXCLUDED.holder,
                   acquired_at  = EXCLUDED.acquired_at,
                   expires_at   = EXCLUDED.expires_at,
                   heartbeat_at = EXCLUDED.heartbeat_at
             WHERE session_locks.expires_at < now()
            RETURNING session_id
            """,
            session_id,
            holder,
            ttl,
        )
        # RETURNING 有没有行，就是「拿没拿到锁」
        return row is not None


async def heartbeat_long_lock(session_id: str, holder: str) -> bool:
    """续期。返回 False 表示锁已不在自己手上（被抢或已释放），调用方应停止生成。"""
    ttl = int(cfg("session.lock_ttl_seconds", 120))
    async with tx() as conn:
        row = await conn.fetchrow(
            """
            UPDATE session_locks
               SET expires_at = now() + make_interval(secs => $3),
                   heartbeat_at = now()
             WHERE session_id = $1 AND holder = $2
            RETURNING session_id
            """,
            session_id,
            holder,
            ttl,
        )
        return row is not None


async def release_long_lock(session_id: str, holder: str) -> bool:
    """释放长锁。

    ⚠️ holder 必须出现在 WHERE 里：
        否则一个已超时、锁被别人抢走的旧请求，在结束时会把**新持有者的锁删掉**——
        新持有者瞬间失去保护，两个生成同时跑。
    """
    async with tx() as conn:
        row = await conn.fetchrow(
            "DELETE FROM session_locks WHERE session_id = $1 AND holder = $2 RETURNING session_id",
            session_id,
            holder,
        )
        return row is not None


async def cleanup_expired_locks() -> int:
    """清理过期行。不是必须的（过期行会被下一个请求直接抢走），
    只是为了让表不无限增长。"""
    async with tx() as conn:
        result = await conn.execute("DELETE FROM session_locks WHERE expires_at < now()")
        # asyncpg 返回 "DELETE n"
        return int(result.split()[-1]) if result.startswith("DELETE") else 0
