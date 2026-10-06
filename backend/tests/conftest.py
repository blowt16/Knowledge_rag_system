"""pytest 共享夹具。

⚠️ 这些测试需要**真实的 PostgreSQL**（§3.2.4 的锁语义只有真库能验）。
   跑之前先：docker compose up -d --wait postgres
"""

from __future__ import annotations

import os
import shutil

import pytest
import pytest_asyncio

from app import db
from app.core.config import repo_path


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir():
    """把数据目录指向**仓库内的 `test_data/`** —— 测试产物不许落进正在用的 `data/`。

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

    ⚠️⚠️ **会话开始时清空上一次的残留，这一步不能省。** 这里的路径是**写死的**，
       不像 `tmp_path_factory` 每次给唯一目录、也不会自动轮换 —— 不清的话，
       跑 40 次又会堆回几千个文件（正是隔离要解决的那个问题）。
       清理发生在**本次运行开始之前**，所以跑挂了仍能去看现场。

    代价：路径写死意味着两个进程同时跑测试会互相踩（CI 与本地同时跑）。
       隔离修复之前测试直接写真实 `data/`，那时并发踩得更死，故非新增风险。

    回归锁见 `integration/test_data_isolation.py`。
    """
    root = repo_path("test_data")
    # 守卫：这个 rmtree 是破坏性的，先确认算出来的确实是我们自己的临时目录
    assert root.name == "test_data" and root.parent == repo_path(), \
        f"拒绝清理非预期的路径：{root}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    previous = os.environ.get("RAG_DATA_DIR")
    os.environ["RAG_DATA_DIR"] = str(root)
    yield root
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
