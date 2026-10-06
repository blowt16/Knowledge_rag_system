"""pytest 共享夹具。

⚠️ 这些测试需要**真实的 PostgreSQL**（§3.2.4 的锁语义只有真库能验）。
   跑之前先：docker compose up -d --wait postgres
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app import db


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir(tmp_path_factory):
    """把数据目录指向临时目录 —— 测试产物不许落进**正在用的** `data/`。

    ⚠️ 为什么必须隔离：摄入会产文件（上传件 / 规范化文本 / 抽图），而
       `purge_documents_by_id` 只清 PG 行 + Chroma + BM25，**不清文件**。
       M5 实测：连续跑两天，真实 `data/` 里堆了约 9300 项 / 124MB 孤儿
       （uploads 3401 / normalized 3396 / extracted_images 2536），
       而库里只有 11 份文档。每跑一次全量测试还会再堆约 85 个。

    ⚠️ 为什么用环境变量而不是 monkeypatch：各调用点写的是
       `from app.core.config import data_dir`，**导入时就绑定了函数对象**，
       patch `config` 模块里的名字对它们无效；而 `data_dir()` 是调用时读
       环境变量的 `RAG_DATA_DIR`。

    ⚠️ 覆盖范围只有 `data/`：`models/`（reranker 权重 2.2GB）与 `logs/`
       走 `repo_path()` 的其他分支，必须仍指向真实位置。

    回归锁见 `integration/test_data_isolation.py`。
    """
    path = tmp_path_factory.mktemp("rag-data")
    previous = os.environ.get("RAG_DATA_DIR")
    os.environ["RAG_DATA_DIR"] = str(path)
    yield path
    if previous is None:
        os.environ.pop("RAG_DATA_DIR", None)
    else:
        os.environ["RAG_DATA_DIR"] = previous


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
