"""pytest 共享夹具。

⚠️ 这些测试需要**真实的 PostgreSQL**（§3.2.4 的锁语义只有真库能验）。
   跑之前先：docker compose up -d --wait postgres
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app import db


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture
async def clean_locks():
    """每个用例前后清掉会话锁行，避免相互污染。"""
    async with db.tx() as conn:
        await conn.execute("DELETE FROM session_locks")
    yield
    async with db.tx() as conn:
        await conn.execute("DELETE FROM session_locks")
