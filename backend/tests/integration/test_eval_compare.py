"""消融对比表接口（§4.3.1.4，M5-2 评测页读的就是这个）。

⚠️ **诊断字段不能混进列名全集**：`ragas` 是嵌套子字典（下面拍平进 values），
   而 `ragas_available` / `ragas_errors` 是「这轮 ragas 跑没跑成、错在哪」的元数据，
   不是消融指标 —— 一个是恒为 true 的布尔、一个是列表，当矩阵列只是噪声。
   它们改为**行上的结构化字段**，前端据此在表下给提示而不是占两列。

⚠️ **四项 ragas 必须排在列首**：这页是消融对比，指标全集有十几个，
   一屏放不下必定横向滚动。ragas 排在末尾 = 默认看不见（M5-2 实测的毛病）。
"""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio

from app import db
from app.core.security import hash_password
from app.main import app

PASSWORD = "Test@12345"
RAGAS_KEYS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin(client):
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, 'admin', 0)",
            user_id, username, hash_password(PASSWORD),
        )
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    yield {"id": user_id, "token": r.json()["access_token"]}
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest_asyncio.fixture
async def run_factory():
    """造 eval_runs 行，用完删干净（eval_case_results 有外键，本文件不建）。"""
    ids: list[str] = []

    async def make(metrics: dict) -> str:
        run_id = uuid.uuid4().hex
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO eval_runs (id, name, config, role, include_restricted,
                                          status, metrics, started_at, finished_at)
                   VALUES ($1, 't', '{}'::jsonb, 'student', 0, 'done', $2, now(), now())""",
                run_id, metrics,
            )
        ids.append(run_id)
        return run_id

    yield make
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_runs WHERE id = ANY($1::text[])", ids)


def _metrics(*, available: bool = True, errors: list[str] | None = None) -> dict:
    """一轮跑完的整轮 metrics —— 形状照 `app.eval.runner._aggregate`。"""
    return {
        "recall_at_k": 0.5,
        "mrr": 0.3,
        "ragas": {k: 0.5 for k in RAGAS_KEYS},
        "ragas_available": available,
        "ragas_errors": errors or [],
    }


async def _compare(client, admin, run_ids: list[str]) -> dict:
    r = await client.get("/api/admin/eval/compare",
                         params={"run_ids": ",".join(run_ids)},
                         headers={"Authorization": f"Bearer {admin['token']}"})
    assert r.status_code == 200, r.text
    return r.json()


async def test_ragas_columns_come_first(client, admin, run_factory):
    rid = await run_factory(_metrics())
    body = await _compare(client, admin, [rid])
    assert body["metrics"][:len(RAGAS_KEYS)] == RAGAS_KEYS


async def test_diagnostics_are_not_columns(client, admin, run_factory):
    rid = await run_factory(_metrics())
    body = await _compare(client, admin, [rid])
    assert "ragas" not in body["metrics"]
    assert "ragas_available" not in body["metrics"]
    assert "ragas_errors" not in body["metrics"]


async def test_diagnostics_are_exposed_on_rows(client, admin, run_factory):
    rid = await run_factory(_metrics(available=False, errors=["ragas 计算超时"]))
    body = await _compare(client, admin, [rid])
    row = body["runs"][0]
    assert row["ragas_available"] is False
    assert row["ragas_errors"] == ["ragas 计算超时"]
