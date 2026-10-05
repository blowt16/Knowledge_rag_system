"""两层会话锁的语义验证（§3.2.4 / §3.3.5）。

这是全文档标注「最容易踩的坑」的一处，逐条按文档给的场景断言：

短锁（事务级 advisory lock）：
  - 加锁与写必须在同一个事务里；事务提交后锁自动释放
长锁（session_locks 表 + TTL）：
  - 空闲则拿、未过期则拒、过期则可抢
  - 释放时 holder 必须匹配，否则会把新持有者的锁误删
"""

from __future__ import annotations

import pytest

from app import db


# ============================================================
# 长锁
# ============================================================

async def test_acquire_when_free(clean_locks):
    assert await db.acquire_long_lock("s1", "holder-A") is True


async def test_second_acquire_rejected_while_held(clean_locks):
    """锁被持有且未过期 → 第二个请求拿不到（这是 409 session_busy 的依据）。"""
    assert await db.acquire_long_lock("s1", "holder-A") is True
    assert await db.acquire_long_lock("s1", "holder-B") is False


async def test_expired_lock_can_be_taken(clean_locks):
    """过期后下一个请求可直接抢走，holder 换新。"""
    assert await db.acquire_long_lock("s1", "holder-A") is True

    async with db.tx() as conn:
        await conn.execute(
            "UPDATE session_locks SET expires_at = now() - interval '1 second' "
            "WHERE session_id = 's1'"
        )

    assert await db.acquire_long_lock("s1", "holder-B") is True

    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT holder FROM session_locks WHERE session_id = 's1'")
        assert row["holder"] == "holder-B"


async def test_old_holder_cannot_release_new_holders_lock(clean_locks):
    """★ 文档点名的坑：holder 必须进 WHERE。

    一个已超时、锁被别人抢走的旧请求，在结束时不能把新持有者的锁删掉——
    否则新持有者瞬间失去保护，两个生成同时跑。
    """
    assert await db.acquire_long_lock("s1", "holder-A") is True

    # A 的锁过期，B 抢走
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE session_locks SET expires_at = now() - interval '1 second' "
            "WHERE session_id = 's1'"
        )
    assert await db.acquire_long_lock("s1", "holder-B") is True

    # A 结束时释放 —— 必须删不掉
    assert await db.release_long_lock("s1", "holder-A") is False

    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT holder FROM session_locks WHERE session_id = 's1'")
        assert row is not None, "新持有者 B 的锁被旧持有者 A 误删了"
        assert row["holder"] == "holder-B"


async def test_correct_holder_releases(clean_locks):
    assert await db.acquire_long_lock("s1", "holder-A") is True
    assert await db.release_long_lock("s1", "holder-A") is True
    # 释放后可立即被再次获取
    assert await db.acquire_long_lock("s1", "holder-B") is True


async def test_heartbeat_extends_expiry(clean_locks):
    from datetime import timedelta

    assert await db.acquire_long_lock("s1", "holder-A") is True

    # 先把到期时间压到 5 秒后，再心跳 —— 这样 before/after 才拉得开差距
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE session_locks SET expires_at = now() + interval '5 seconds' "
            "WHERE session_id = 's1'")
    async with db.tx() as conn:
        before = await conn.fetchval(
            "SELECT expires_at FROM session_locks WHERE session_id = 's1'")

    assert await db.heartbeat_long_lock("s1", "holder-A") is True

    async with db.tx() as conn:
        after = await conn.fetchval(
            "SELECT expires_at FROM session_locks WHERE session_id = 's1'")
    # 心跳把 TTL 续回 120 秒（配置值），应从 now+5s 明显推后
    assert after > before + timedelta(seconds=60), "心跳没有延长 expires_at"


async def test_heartbeat_fails_when_lock_lost(clean_locks):
    """锁被抢走后，原持有者续期应失败 —— 调用方据此停止生成。"""
    assert await db.acquire_long_lock("s1", "holder-A") is True
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE session_locks SET expires_at = now() - interval '1 second' "
            "WHERE session_id = 's1'")
    assert await db.acquire_long_lock("s1", "holder-B") is True
    assert await db.heartbeat_long_lock("s1", "holder-A") is False


async def test_cleanup_expired_removes_only_expired(clean_locks):
    await db.acquire_long_lock("s1", "A")
    await db.acquire_long_lock("s2", "A")
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE session_locks SET expires_at = now() - interval '1 second' "
            "WHERE session_id = 's1'")

    removed = await db.cleanup_expired_locks()
    assert removed == 1

    async with db.tx() as conn:
        ids = [r["session_id"] for r in await conn.fetch("SELECT session_id FROM session_locks")]
    assert ids == ["s2"]


# ============================================================
# 短锁：advisory lock 必须是事务级的（§3.3.5）
# ============================================================

async def test_short_lock_held_within_transaction():
    """★ 验证 pg_advisory_xact_lock 在事务期间确实持有。

    这条直接对应文档那句：「加锁 → 写 → 提交必须同一个事务」。
    """
    pool = db.get_pool()
    async with pool.acquire() as conn_a:
        async with conn_a.transaction():
            await db.short_lock(conn_a, "s1")

            # 另一个连接尝试拿同一把锁 —— 应该拿不到
            async with pool.acquire() as conn_b:
                got = await conn_b.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext('session:' || $1))", "s1")
                assert got is False, "事务仍在进行，锁却被另一个连接拿到了"

        # 事务已提交 → 锁应已自动释放
        async with pool.acquire() as conn_c:
            got = await conn_c.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('session:' || $1))", "s1")
            assert got is True, "事务已提交，锁却没有释放"


async def test_advisory_lock_scoped_per_session():
    """不同 session_id 的短锁互不阻塞。"""
    pool = db.get_pool()
    async with pool.acquire() as conn_a:
        async with conn_a.transaction():
            await db.short_lock(conn_a, "s1")
            async with pool.acquire() as conn_b:
                got = await conn_b.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext('session:' || $1))", "s2")
                assert got is True, "不同会话的锁不应互相阻塞"
