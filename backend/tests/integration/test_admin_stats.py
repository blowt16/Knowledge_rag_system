"""管理端统计（§3.7.3 / §4.4 仪表盘）。

⚠️ **`refusal_rate` 的口径是 M4-D2 定的**（写进计划文档 §9.0）：
   分母是 **`route='knowledge'` 的轮次**，不是全部轮次。
   理由：闲聊/澄清轮次根本没有「证据面」可言，把几百条闲聊算进分母，
   会把拒答率稀释成一个没有意义的偏低的数。
   §3.7.3 说「定义与分母见 5.2」，而 §5.2 里**只有评测集的漏答率/误答率** ——
   这是文档缺口，本文件锁的就是定下来的那条口径。

⚠️ **`trend` 必须按天补零**：没有问答的那天也要出现，否则折线图会断开
   （§3.7.3 的响应说明）。

⚠️ **`stats/retrieval` 在 M4 没有 Prometheus**，必须返回
   `{available: false}` 且 **HTTP 200** —— §4.4 明文：运行指标不可用时
   **业务指标区块照常渲染**，所以这里不能 500（M4-D5）。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import httpx
import pytest
import pytest_asyncio

from app import db
from app.core.security import hash_password
from app.main import app

PASSWORD = "Test@12345"


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _insert_user(role: str) -> dict:
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, $4, 0)",
            user_id, username, hash_password(PASSWORD), role,
        )
    return {"id": user_id, "username": username}


async def _login(client, username: str) -> str:
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin(client):
    u = await _insert_user("admin")
    u["token"] = await _login(client, u["username"])
    yield u
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


@pytest_asyncio.fixture
async def logs(admin):
    """本用例造的所有 qa_logs / degradation_events，用完清干净。"""
    created: dict[str, list[str]] = {"qa": [], "deg": []}

    async def add_qa(*, route: str, refused: bool, question: str = "q",
                     days_ago: int = 0) -> str:
        log_id = uuid.uuid4().hex
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO qa_logs (id, session_id, user_id, user_role, question,
                                        route, is_refused, refusal_reason, created_at)
                   VALUES ($1, 's', $2, 'student', $3, $4, $5, $6,
                           now() - ($7 || ' days')::interval)""",
                log_id, admin["id"], question, route, 1 if refused else 0,
                "no_candidate" if refused else None, str(days_ago))
        created["qa"].append(log_id)
        return log_id

    async def add_degradation(kind: str, node: str = "rerank") -> None:
        deg_id = uuid.uuid4().hex
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO degradation_events (id, session_id, node, kind, detail)
                   VALUES ($1, 's', $2, $3, 'test')""",
                deg_id, node, kind)
        created["deg"].append(deg_id)

    yield {"add_qa": add_qa, "add_degradation": add_degradation,
           "ids": created}

    async with db.tx() as conn:
        if created["qa"]:
            await conn.execute("DELETE FROM qa_logs WHERE id = ANY($1)", created["qa"])
        if created["deg"]:
            await conn.execute("DELETE FROM degradation_events WHERE id = ANY($1)",
                               created["deg"])


# ============================================================
# overview
# ============================================================

async def test_overview_shape_and_counts(client, admin, logs):
    r = await client.get("/api/admin/stats/overview", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    body = r.json()

    for key in ("document_count", "chunk_count", "qa_count", "refusal_rate",
                "degradation_counts"):
        assert key in body, f"overview 缺少 {key}"

    # 与库里真实的「当前生效」口径对一遍（而不是写死数字）
    async with db.tx() as conn:
        expected_docs = await conn.fetchval("""
            SELECT count(*) FROM documents d
             WHERE d.status = 'active' AND d.effective_date <= CURRENT_DATE
               AND d.version = (SELECT max(d2.version) FROM documents d2
                                 WHERE d2.doc_group_id = d.doc_group_id
                                   AND d2.status = 'active'
                                   AND d2.effective_date <= CURRENT_DATE)""")
    assert body["document_count"] == expected_docs
    assert body["chunk_count"] > 0


async def test_overview_degradation_counts_grouped_by_kind(client, admin, logs):
    await logs["add_degradation"]("timeout")
    await logs["add_degradation"]("timeout")
    await logs["add_degradation"]("oom")

    r = await client.get("/api/admin/stats/overview", headers=_h(admin["token"]))

    counts = r.json()["degradation_counts"]
    assert counts.get("timeout", 0) >= 2
    assert counts.get("oom", 0) >= 1


# ============================================================
# refusal_rate 的口径（M4-D2）
# ============================================================

async def _rate(client, token) -> float:
    r = await client.get("/api/admin/stats/overview", headers=_h(token))
    return r.json()["refusal_rate"]


async def test_refusal_rate_ignores_chat_rounds(client, admin, logs):
    """★ 闲聊轮次**不进分母** —— 否则问几句「你好」就能把拒答率稀释掉。"""
    before = await _rate(client, admin["token"])

    for _ in range(5):
        await logs["add_qa"](route="chat", refused=True)

    after = await _rate(client, admin["token"])

    assert after == pytest.approx(before), (
        "闲聊轮次改变了拒答率 —— 分母不是 route='knowledge' 的轮次"
    )


async def test_refusal_rate_matches_knowledge_only_definition(client, admin, logs):
    await logs["add_qa"](route="knowledge", refused=True)
    await logs["add_qa"](route="knowledge", refused=False)

    async with db.tx() as conn:
        refused = await conn.fetchval(
            "SELECT count(*) FROM qa_logs WHERE is_refused = 1 AND route = 'knowledge'")
        total = await conn.fetchval(
            "SELECT count(*) FROM qa_logs WHERE route = 'knowledge'")

    rate = await _rate(client, admin["token"])
    assert rate == pytest.approx(refused / total)


# ============================================================
# trend
# ============================================================

async def test_trend_zero_fills_missing_days(client, admin, logs):
    """★ 没有问答的那天也必须在 `days` 里 —— 否则折线图会断开。

    ⚠️ 判据**不能**建立在「某一天恰好没有数据」上：库里本来就有历史日志
       （本机就有一批昨天的），挑哪一天都可能踩到。真正能区分
       「补零」与「只返回有数据的那几天」的是**结构**：
       前者恒返回 N 天且逐日连续，后者会少几天。
    """
    days_back = 7
    await logs["add_qa"](route="knowledge", refused=False, days_ago=0)
    await logs["add_qa"](route="knowledge", refused=True, days_ago=3)

    r = await client.get(f"/api/admin/stats/trend?days={days_back}",
                         headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    days = r.json()["days"]

    assert len(days) == days_back, f"请求 {days_back} 天就该回 {days_back} 天，实为 {len(days)}"
    dates = [date.fromisoformat(d["date"]) for d in days]
    assert dates == sorted(dates), "日期要升序"
    assert dates[-1] == date.today(), "最后一天必须是今天"
    assert dates[0] == date.today() - timedelta(days=days_back - 1)
    assert all((dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1)), \
        "日期必须逐日连续 —— 中间缺席的那天正是「没补零」的表现"

    assert days[-1]["qa_count"] >= 1
    three_ago = next(d for d in days
                     if d["date"] == (date.today() - timedelta(days=3)).isoformat())
    assert three_ago["refusal_count"] >= 1


# ============================================================
# refusals 聚合 / hot-questions
# ============================================================

async def test_refusals_aggregates_by_reason_and_top_questions(client, admin, logs):
    q = f"聚合测试问题_{uuid.uuid4().hex[:8]}"
    await logs["add_qa"](route="knowledge", refused=True, question=q)
    await logs["add_qa"](route="knowledge", refused=True, question=q)

    r = await client.get("/api/admin/stats/refusals", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"by_reason", "top_questions"} == set(body)

    reasons = {x["reason"] for x in body["by_reason"]}
    assert reasons <= {"no_candidate", "insufficient_evidence"}, \
        "reason 取值必须用 §5.2 的词表，不得自造"
    assert any(x["question"] == q and x["count"] >= 2 for x in body["top_questions"])


async def test_hot_questions_is_top_10_by_default(client, admin, logs):
    """默认 Top 10（§3.7.3）—— 按次数倒序。"""
    tag = uuid.uuid4().hex[:8]
    for i in range(12):
        # 第 i 个问题问 (12-i) 次 → 次数越大的问题编号越小
        for _ in range(12 - i):
            await logs["add_qa"](route="knowledge", refused=False,
                                 question=f"热问_{tag}_{i}")

    r = await client.get("/api/admin/stats/hot-questions", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    items = r.json()["items"]

    assert len(items) == 10, f"默认 Top 10，实为 {len(items)}"
    counts = [x["count"] for x in items]
    assert counts == sorted(counts, reverse=True), "必须按次数倒序"
    assert items[0]["question"] == f"热问_{tag}_0"


# ============================================================
# retrieval（运行指标）
# ============================================================

async def test_retrieval_is_200_and_unavailable_without_prometheus(client, admin):
    """★ M4 没有 Prometheus —— 必须 `available:false` + **HTTP 200**（M4-D5）。

    返 500 的话前端整个仪表盘会白屏，而 §4.4 要求**业务指标区块照常渲染**。
    """
    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, "不可用不是错误，不能 500"
    body = r.json()
    assert body["available"] is False
    assert body.get("status") == "prometheus_unavailable"


async def test_retrieval_returns_metrics_when_prometheus_answers(client, admin,
                                                                monkeypatch):
    """Prometheus 能答上时的分支 —— 用 MockTransport 走**真实的 HTTP 代码路径**，
    而不是把内部函数打桩（打桩只能证明自己调了自己）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "vector", "result": [{"value": [0, "0.42"]}]},
        })

    monkeypatch.setattr("app.services.stats_service.PROMETHEUS_URL", "http://prom.test")
    monkeypatch.setattr("app.services.stats_service._prom_transport",
                        httpx.MockTransport(handler))

    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["latency_p95"] is not None or body["error_rate"] is not None


async def test_student_cannot_read_stats(client):
    u = await _insert_user("student")
    token = await _login(client, u["username"])
    try:
        r = await client.get("/api/admin/stats/overview", headers=_h(token))
        assert r.status_code == 403, r.text
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", u["id"])
