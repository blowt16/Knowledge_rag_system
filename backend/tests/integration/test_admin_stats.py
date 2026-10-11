"""管理端统计（§3.7.3 / §4.4 仪表盘）。

⚠️ **`refusal_rate` 的口径是 M4-D2 定的**（写进计划文档 §9.0）：
   分母是 **`route='knowledge'` 的轮次**，不是全部轮次。
   理由：闲聊/澄清轮次根本没有「证据面」可言，把几百条闲聊算进分母，
   会把拒答率稀释成一个没有意义的偏低的数。
   §3.7.3 说「定义与分母见 5.2」，而 §5.2 里**只有评测集的漏答率/误答率** ——
   这是文档缺口，本文件锁的就是定下来的那条口径。

⚠️ **`trend` 必须按天补零**：没有问答的那天也要出现，否则折线图会断开
   （§3.7.3 的响应说明）。

⚠️ **`stats/retrieval` 读的是 Langfuse**（Task 10 起换到 Metrics API v2）：
   Langfuse 未配 / 不可达 / 窗口内无数据时返回 `{available: false}` 且 **HTTP 200**
   —— §4.4 明文：运行指标不可用时**业务指标区块照常渲染**，所以这里不能 500（M4-D5）。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import httpx
import pytest
import pytest_asyncio

from app import db
from app.core.security import hash_password
from app.main import app
from app.services import stats_service

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
# retrieval（运行指标）—— Task 10 起读 Langfuse Metrics API v2
# ============================================================

# 夹具里用的度量值：**每个指标一个不可能撞车的数**。
# ⚠️ 不能只断言「有值/相等」而不挑数：Langfuse SDK 本身架在 OTel 上，
#    两条读取路径（Prometheus 时代 vs 现在）会返回逐字节相同的值，
#    用通用值断言等于什么都没验。这里每个数只可能在夹具里出现一次。
_V2_LATENCY_P50 = 250.5
_V2_LATENCY_P95 = 1300.5
_V2_ERROR_COUNTS = {"DEFAULT": 90, "ERROR": 10}      # → 10% ，越过 5% 阈值
_V2_TOKENS_TOTAL = 4321          # = input + output（照实测的 10 = 7 + 3 同构）
_V2_TOKENS_IN = 1234             # ⚠️ 刻意与总量不同：退化成副本（三字段同值）当场红
_V2_TOKENS_OUT = 3087
_V2_COST = 0.25
_V2_NODE_P95 = {
    "node.rerank": 15000.0,        # > 10s 阈值 → firing
    "node.retrieve": 120.0,        # 不越 → inactive
    "not-a-node-span": 99999.0,    # 不叫 node.*，不该进面板或告警规则
}


def _v2_payload(request: httpx.Request) -> dict:
    """按 `query` 参数里要的度量/维度，回**实测形状**的 v2 响应体。"""
    query = json.loads(request.url.params["query"])
    measures = [m["measure"] for m in query["metrics"]]
    dims = [d["field"] for d in query.get("dimensions") or []]
    if "totalTokens" in measures:
        # ⚠️ 实测形状（2026-10-11 真项目查得）：**一行**，三个 measure 分列
        #    （input 7 / output 3 / total 10）—— 拆 token 靠的是**分开的
        #    measure**，不是 `usageType` 维度（那个只会把同一个总量回成三行）。
        #    夹具照抄，且刻意让 input ≠ total：实现退化成副本当场红。
        return {"data": [{"sum_inputTokens": _V2_TOKENS_IN,
                          "sum_outputTokens": _V2_TOKENS_OUT,
                          "sum_totalTokens": _V2_TOKENS_TOTAL,
                          "sum_totalCost": _V2_COST}]}
    if dims == ["name"]:
        return {"data": [{"name": n, "p95_latency": v} for n, v in _V2_NODE_P95.items()]}
    if dims == ["level"]:
        return {"data": [{"level": k, "count_count": v}
                         for k, v in _V2_ERROR_COUNTS.items()]}
    # 不分组：延迟
    return {"data": [{"count_count": 100, "p50_latency": _V2_LATENCY_P50,
                      "p95_latency": _V2_LATENCY_P95}]}


def _langfuse_configured(monkeypatch) -> None:
    """配上假密钥（**环境变量**）—— `stats_service` 是调用时读的，与 conftest
    的 `_never_talk_to_langfuse` 那道闸配套：忘了配的用例真的走不到云端。"""
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


def _langfuse_answering(monkeypatch, payload=_v2_payload) -> None:
    """配上假密钥 + MockTransport，让「答得上」这条分支走**真实的 HTTP 代码路径**
    （而不是把内部函数打桩 —— 打桩只能证明自己调了自己）。"""
    _langfuse_configured(monkeypatch)
    monkeypatch.setattr(
        "app.services.stats_service._lf_transport",
        httpx.MockTransport(lambda request: httpx.Response(200, json=payload(request))))


async def test_retrieval_is_200_and_unavailable_without_langfuse(client, admin):
    """★ Langfuse 未配时必须 `available:false` + **HTTP 200**（M4-D5）。

    返 500 的话前端整个仪表盘会白屏，而 §4.4 要求**业务指标区块照常渲染**。

    ⚠️ 这里**不用**自己清密钥：conftest 的 `_never_talk_to_langfuse` 已经删了
       `LANGFUSE_*`，而 `stats_service` 是调用时读环境变量的 —— 闸真的生效。
    """
    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, "不可用不是错误，不能 500"
    body = r.json()
    assert body["available"] is False
    assert body.get("status") == "langfuse_unavailable"


async def test_retrieval_returns_metrics_when_langfuse_answers(client, admin,
                                                              monkeypatch):
    """Langfuse 能答上时的分支 —— 四个查询各自返回值，逐个对号。"""
    _langfuse_answering(monkeypatch)

    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["latency_p50"] == _V2_LATENCY_P50
    assert body["latency_p95"] == _V2_LATENCY_P95
    assert body["error_rate"] == pytest.approx(
        _V2_ERROR_COUNTS["ERROR"] / sum(_V2_ERROR_COUNTS.values()))   # ERROR 10/100
    assert body["token_usage"] == _V2_TOKENS_TOTAL


async def test_retrieval_reports_firing_alerts(client, admin, monkeypatch):
    """告警状态（§4.4）—— 状态卡「有 firing 即标红」的数据源。

    ⚠️ 改**后端自算**（【方案 §8.3】方案 A）：Langfuse 告警没有公开 API
       （6 个候选端点全 404），所以阈值比较落在后端。返回形状与改造前一致
       （`{firing, pending, rules:[{name,state}]}`），前端不用改。
    """
    _langfuse_answering(monkeypatch)

    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, r.text
    alerts = r.json()["alerts"]
    assert alerts["pending"] == 0, "自算没有 pending 态"
    assert alerts["firing"] == 2, "rerank p95 越限 + 错误率越限"
    # 节点规则按 p95 倒序，`not-a-node-span` 不在其中（只统计 `node.*`）
    assert [x["name"] for x in alerts["rules"]] == [
        "RAGNodeP95High[rerank]", "RAGNodeP95High[retrieve]", "RAGHttp5xxRate"]
    assert [x["state"] for x in alerts["rules"]] == ["firing", "inactive", "firing"]


async def test_retrieval_service_exposes_cost_and_node_latency(monkeypatch):
    """`cost` / `node_latency`（§8.2 新增面板）—— 在 **service 层**锁字段名与解析。

    ⚠️ 为什么不走 HTTP：`RetrievalMetricsResponse` 要到 Task 12 才补这两个字段，
       Pydantic v2 默认 ignore 多余键 → 这一层还看不见它们。但字段名
       （`sum_totalCost`、`node.*` 前缀）是**实测来的**，取错了面板恒显示「—」，
       所以现在就得锁住。
    """
    _langfuse_answering(monkeypatch)

    body = await stats_service.retrieval_metrics()

    assert body["cost"] == _V2_COST, "成本与 token 同一行：取该行的 sum_totalCost"
    assert body["node_latency"] == [{"node": "rerank", "p95": 15000.0},
                                    {"node": "retrieve", "p95": 120.0}]


async def test_retrieval_service_splits_tokens_by_measure(monkeypatch):
    """token 拆分走**三个独立 measure**（§8.2 的「拆输入/输出」）—— service 层锁住。

    ⚠️ 夹具是**实测形状**（一行三字段，见 `_v2_payload`），三个数**互不相同**：
       实现若退化成「三个字段读同一个值」（`usageType` 时代那种副本），
       `input != total` 当场红。历史坑：`dimensions=[usageType]` 回三行副本，
       全加会变成 3 倍 —— 现在维度整个不用了。
    """
    _langfuse_answering(monkeypatch)

    body = await stats_service.retrieval_metrics()

    assert body["token_usage"] == _V2_TOKENS_TOTAL
    assert body["token_input"] == _V2_TOKENS_IN
    assert body["token_output"] == _V2_TOKENS_OUT
    assert body["token_input"] != body["token_usage"], "拆分退化成副本必须红"


async def test_retrieval_available_even_before_any_span(client, admin, monkeypatch):
    """★ Langfuse 答得上、但窗口内**一条数据都没有**（刚起服务、还没人来提问）时，
    必须是 `available: true` —— **接好了、只是还没流量**。

    报 false 的话，仪表盘会把「已接好、只是还没流量」显示成「暂不可用」，
    让人以为接错了、跑去查配置 —— 改造前的代码专门写过注释避免这件事。

    ⚠️ 判据是**「有没有一个查询真的答上了」**，不是「有没有非空值」：
       `_lf_query` 只在**失败**时返回 None；答上了但窗口是空的会回一个 payload
       （两种空结果形态这里都造了：字段是 `null` / 空数组）。两者必须分开，
       否则「全空当不可用」与「全失败当可用」两种错法各能骗过一条用例。
    """
    def payload(request: httpx.Request) -> dict:
        query = json.loads(request.url.params["query"])
        if query.get("dimensions"):
            return {"data": []}                      # 空结果形态二：空数组
        return {"data": [{"count_count": 0,         # 空结果形态一：字段是 null 不是 0
                          "p50_latency": None, "p95_latency": None}]}

    _langfuse_answering(monkeypatch, payload)

    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True, "答上了就算可用，与窗口里有没有数据无关"
    assert body["latency_p50"] is None
    assert body["latency_p95"] is None
    assert body["token_usage"] is None
    assert body["alerts"] == {"firing": 0, "pending": 0,
                              "rules": [{"name": "RAGHttp5xxRate",
                                         "state": "inactive"}]}


async def test_retrieval_unavailable_when_langfuse_errors(client, admin, monkeypatch):
    """★ 反方向：四个查询**全部失败**时才是 `available: false`，且**仍是 HTTP 200**。

    两种失败都造：连不上（T14 验收 B8 那条：`LANGFUSE_BASE_URL` 指向不存在的地址）
    与云上回错（5xx），两者走的都是 `_lf_query` 的 except 分支。
    """
    _langfuse_configured(monkeypatch)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    def http_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    for label, handler in (("连不上", unreachable), ("5xx", http_500)):
        monkeypatch.setattr("app.services.stats_service._lf_transport",
                            httpx.MockTransport(handler))

        r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

        assert r.status_code == 200, "不可用不是错误，不能 500"
        body = r.json()
        assert body["available"] is False, f"{label} 时该报不可用"
        assert body.get("status") == "langfuse_unavailable", f"{label} 时 status 不对"


async def test_retrieval_available_when_only_some_metrics_present(client, admin,
                                                                  monkeypatch):
    """正控：答得上时，**只要 `level` 有行**，`error_rate` 就该是 `0.0` 而不是 `None`。

    ⚠️ `0.0` 是**有值**：它决定「一次错都没有」与「这窗口没数据」在告警上的区别
       （前者正常、后者不误报）。可用性本身看的是**有没有查询答上**，与值无关。
    """
    def payload(request: httpx.Request) -> dict:
        query = json.loads(request.url.params["query"])
        dims = [d["field"] for d in query.get("dimensions") or []]
        if dims == ["level"]:
            return {"data": [{"level": "DEFAULT", "count_count": 7}]}
        return {"data": []}

    _langfuse_answering(monkeypatch, payload)

    r = await client.get("/api/admin/stats/retrieval", headers=_h(admin["token"]))

    body = r.json()
    assert body["available"] is True, "有任一指标有值就不算不可用"
    assert body["latency_p95"] is None
    assert body["error_rate"] == 0.0
    assert body["alerts"] == {"firing": 0, "pending": 0,
                              "rules": [{"name": "RAGHttp5xxRate",
                                         "state": "inactive"}]}


async def test_student_cannot_read_stats(client):
    u = await _insert_user("student")
    token = await _login(client, u["username"])
    try:
        r = await client.get("/api/admin/stats/overview", headers=_h(token))
        assert r.status_code == 403, r.text
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


# ============================================================
# 评审 I-1：trend 的「一天」必须按**本地**时区切
# ============================================================

async def test_trend_buckets_by_local_day_not_utc(client, admin, logs):
    """★ 评审实测发现：PG 会话时区是 **UTC** 而进程本地是 **+08:00**，
    于是本地 00:00–07:59 的问答全被算进**前一天** —— 「今天」在早上永远显示 0。

    判据：在**本地今天凌晨 02:00** 落一条，它必须出现在今天那一格里。
    （本地 0–8 点跑这条用例时，按 UTC 分桶会把它落到昨天，用例立刻红。）
    """
    from datetime import datetime, time as dtime
    local_2am = datetime.combine(date.today(), dtime(2, 0)).astimezone()

    log_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO qa_logs (id, session_id, user_id, user_role, question,
                                    route, is_refused, created_at)
               VALUES ($1, 's', $2, 'student', '凌晨的问题', 'knowledge', 0, $3)""",
            log_id, admin["id"], local_2am)
    logs["ids"]["qa"].append(log_id)

    r = await client.get("/api/admin/stats/trend?days=3", headers=_h(admin["token"]))
    days = r.json()["days"]
    today_row = next(d for d in days if d["date"] == date.today().isoformat())

    assert today_row["qa_count"] >= 1, (
        f"本地今天凌晨的问答没算进今天 —— 分桶用的不是本地时区：{days}"
    )
