"""管理端统计（§3.7.3 / §4.4 仪表盘）。

**两块数据源，别混**（§4.4）：
- **业务指标**读 PostgreSQL（本模块的 overview / trend / refusals / hot-questions）；
- **运行指标**用 Metrics API v2 查 Langfuse（【方案 §8.1】）（只有 `retrieval_metrics`）。

⚠️ **`refusal_rate` 的口径是 M4-D2 定的**（计划文档 §9.0）：分母是
   **`route='knowledge'` 的轮次**。§3.7.3 说「定义与分母见 5.2」，而 §5.2 里
   **只有评测集的漏答率/误答率** —— 文档缺口，口径落在这里。
   闲聊/澄清轮次没有证据面，计入分母会把拒答率稀释成没有意义的偏低值。

⚠️ **`qa_count` 不含评测轮次**：评测结果落 `eval_case_results`，天然不进
   业务口径。**若 M5 让评测也写 `qa_logs`，必须在这里加过滤**（已写进 §9.0）。

⚠️ **不可用不是错误**：`LANGFUSE_*` 未配、或云上查询失败/无数据时，
   `retrieval_metrics` 返回 `{available: false, status: "langfuse_unavailable"}`，
   由 api 层照常返 **HTTP 200** —— §4.4 要求运行指标不可用时
   **业务指标区块照常渲染**，返 500 会把整个仪表盘打白。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone

import httpx

from app import db
from app.core.config import cfg

logger = logging.getLogger(__name__)

# ⚠️ Langfuse 配置**在调用时读**（见 `_lf_config`），不在 import 时快照 ——
#    模块级常量会把 `.env` 里的真 key 钉死，conftest 的 `_never_talk_to_langfuse`
#    只删环境变量、够不着常量，于是**忘了打桩的测试会真的打到云上**。
#    `core/observability.py` 也是调用时读，口径一致。
# ⚠️ 实测 v1 接口对本组织全部 410，一切走 v2（【方案 §12.4】）。

# 测试注入用（httpx.MockTransport）—— 让「答得上」这条分支走**真实 HTTP 代码路径**
_lf_transport: httpx.BaseTransport | None = None

# 超时要短：仪表盘是给人看的，Langfuse 慢不该拖垮页面
_LF_TIMEOUT = 3.0

# 「当前生效」口径与 `document_service._CURRENT_VERSION_SQL` **同一份**（§3.3.1）
_CURRENT_VERSION_SQL = """
    d.status = 'active'
    AND d.effective_date <= CURRENT_DATE
    AND d.version = (
        SELECT max(d2.version) FROM documents d2
         WHERE d2.doc_group_id = d.doc_group_id
           AND d2.status = 'active' AND d2.effective_date <= CURRENT_DATE
    )
"""

# 拒答原因词表（§5.2，唯一来源）
REFUSAL_REASONS = ("no_candidate", "insufficient_evidence")

TOP_N = 10


async def overview() -> dict:
    """核心指标卡（§4.4）：文档数 / chunk 数 / 问答量 / 拒答率 / 降级次数。

    口径：**文档数与 chunk 数只算「当前生效」的那一版** —— 一个制度传了 3 版
    不该在卡片上显示成 3 份文档。两处口径必须与检索期的版本折叠一致，
    否则管理员看到的数与学生实际检索到的对不上。
    """
    async with db.tx() as conn:
        row = await conn.fetchrow(
            f"""SELECT
                  (SELECT count(*) FROM documents d WHERE {_CURRENT_VERSION_SQL})
                      AS document_count,
                  (SELECT coalesce(sum(d.chunk_count), 0) FROM documents d
                    WHERE {_CURRENT_VERSION_SQL}) AS chunk_count,
                  (SELECT count(*) FROM qa_logs) AS qa_count,
                  (SELECT count(*) FROM qa_logs WHERE route = 'knowledge')
                      AS knowledge_count,
                  (SELECT count(*) FROM qa_logs
                    WHERE route = 'knowledge' AND is_refused = 1)
                      AS knowledge_refused""")
        degradations = await conn.fetch(
            "SELECT kind, count(*) AS n FROM degradation_events GROUP BY kind")

    total = row["knowledge_count"] or 0
    refused = row["knowledge_refused"] or 0
    return {
        "document_count": row["document_count"],
        "chunk_count": row["chunk_count"],
        "qa_count": row["qa_count"],
        "refusal_rate": (refused / total) if total else 0.0,
        "degradation_counts": {r["kind"]: r["n"] for r in degradations},
    }


async def trend(*, days: int = 30) -> dict:
    """问答量趋势（§3.7.3）：**按天补零**。

    没有问答的那天也要出现 —— 缺席的那天在折线图上表现为断开，
    看上去像「服务挂过一天」。

    ⚠️ **「一天」按本地时区切**（评审 I-1 实测）：PG 会话时区是 **UTC**，
       而进程本地是 **+08:00** —— 用 `date_trunc('day', created_at)` 分桶，
       本地 00:00–07:59 的每一次问答都会被算进**前一天**，
       「今天」在早上永远显示 0。所以窗口与分桶都在 Python 里按本地日算，
       SQL 只负责按**绝对时刻**取窗口（`timestamptz` 比较与会话时区无关）。

    数据量有界（一个校园系统的问答日志），取回本地过滤比在 SQL 里
       拼时区表达更不容易错。
    """
    today = date.today()
    first = today - timedelta(days=days - 1)
    # 本地零点对应的**绝对时刻**
    window_start = datetime.combine(first, time.min).astimezone()

    async with db.tx() as conn:
        rows = await conn.fetch(
            """SELECT created_at, is_refused FROM qa_logs
                WHERE created_at >= $1""",
            window_start)

    buckets: dict[date, list[int]] = {
        first + timedelta(days=i): [0, 0] for i in range(days)
    }
    for r in rows:
        # asyncpg 给的是 UTC-aware；astimezone() 不带参数 → 转本机时区
        day = r["created_at"].astimezone().date()
        slot = buckets.get(day)
        if slot is None:
            continue
        slot[0] += 1
        if r["is_refused"]:
            slot[1] += 1

    return {"days": [
        {"date": day.isoformat(), "qa_count": counts[0], "refusal_count": counts[1]}
        for day, counts in sorted(buckets.items())
    ]}


async def refusals_aggregate() -> dict:
    """拒答**聚合**（饼图 + Top N 计数）—— 明细见 `GET /api/admin/refusals`。"""
    async with db.tx() as conn:
        by_reason = await conn.fetch(
            """SELECT refusal_reason AS reason, count(*) AS count
                 FROM qa_logs WHERE is_refused = 1 AND refusal_reason IS NOT NULL
                GROUP BY refusal_reason ORDER BY count DESC""")
        top = await conn.fetch(
            """SELECT question, count(*) AS count FROM qa_logs
                WHERE is_refused = 1 AND question IS NOT NULL
                GROUP BY question ORDER BY count DESC, question LIMIT $1""",
            TOP_N)
    return {
        "by_reason": [{"reason": r["reason"], "count": r["count"]} for r in by_reason],
        "top_questions": [{"question": r["question"], "count": r["count"]} for r in top],
    }


async def hot_questions(*, limit: int = TOP_N) -> dict:
    """高频问题（§3.7.3）—— 默认 Top 10。"""
    async with db.tx() as conn:
        rows = await conn.fetch(
            """SELECT question, count(*) AS count FROM qa_logs
                WHERE question IS NOT NULL
                GROUP BY question ORDER BY count DESC, question LIMIT $1""",
            limit)
    return {"items": [{"question": r["question"], "count": r["count"]} for r in rows]}


def _build_metrics_query(*, measures: list[tuple[str, str]],
                         dimensions: list[str] | None, hours: int) -> str:
    """拼 Metrics API v2 的 `query` 参数（**一个 JSON 字符串**，实测形状）。

    ⚠️ `fromTimestamp` / `toTimestamp` 是**必填**的（不传直接 400）。
    ⚠️ 窗口一律取「相对当前时间」—— 实测传很久以前的绝对日期会报
       「fromTimestamp must be before toTimestamp」（**报错信息有误导性**）。
    """
    now = datetime.now(timezone.utc)
    query: dict = {
        "view": "observations",
        "metrics": [{"measure": m, "aggregation": a} for m, a in measures],
        "fromTimestamp": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if dimensions:
        query["dimensions"] = [{"field": d} for d in dimensions]
    return json.dumps(query)


def _extract_scalar(payload: dict | None, key: str) -> float | None:
    """从 v2 返回里取一个标量。

    ⚠️ **两种空结果都要扛住**（实测）：
       ① `{"data":[{"count_count":0,"p95_latency":null}]}` —— 字段是 `null`
       ② `{"data":[]}` —— 连行都没有
       直接 `float(row[key])` 会在①上抛 TypeError，被 swallow 后变成静默的「—」。
    """
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    value = rows[0].get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lf_config() -> tuple[str, str, str] | None:
    """取 Langfuse 配置；三项缺任一返回 None（→ 走不可用分支）。

    ⚠️ **调用时读环境变量，不在 import 时快照**：否则测试里 conftest 那道
       「绝不往 Langfuse 发数据」的闸（只删环境变量）对忘了打桩的用例失效。
    """
    base_url = (os.environ.get("LANGFUSE_BASE_URL", "").strip()
                or cfg("observability.langfuse_base_url", "") or "")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (base_url and public_key and secret_key):
        return None
    return base_url, public_key, secret_key


async def _lf_query(query_json: str) -> dict | None:
    """查一次 Metrics API v2。任何失败返回 None（由调用方决定怎么降级）。"""
    conf = _lf_config()
    if conf is None:
        return None
    base_url, public_key, secret_key = conf
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            auth=(public_key, secret_key),
            timeout=_LF_TIMEOUT, transport=_lf_transport,
        ) as client:
            resp = await client.get("/api/public/v2/metrics",
                                    params={"query": query_json})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001 —— 运行指标缺失不该让页面挂掉
        logger.warning("Langfuse 查询失败：%s", e,
                       extra={"event": "stats.langfuse_failed"})
        return None


def _sum_rows(payload: dict | None, key: str) -> float | None:
    rows = (payload or {}).get("data") or []
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return float(sum(vals)) if vals else None


def _error_rate_from_levels(payload: dict | None) -> float | None:
    """按 level 分组算错误率（【方案 §8.3】实测的替代方案）。"""
    rows = (payload or {}).get("data") or []
    total = sum(r.get("count_count") or 0 for r in rows)
    if not total:
        return None
    errs = sum(r.get("count_count") or 0 for r in rows if r.get("level") == "ERROR")
    return errs / total


def _node_latency_rows(payload: dict | None) -> list[dict]:
    """按节点分组的 p95（§8.2 新增面板）。"""
    rows = (payload or {}).get("data") or []
    out = []
    for r in rows:
        name = r.get("name") or ""
        val = r.get("p95_latency")
        if name.startswith("node.") and val is not None:
            out.append({"node": name[len("node."):], "p95": float(val)})
    return sorted(out, key=lambda x: x["p95"], reverse=True)


def _evaluate_alerts(values: dict) -> dict:
    """告警后端自算（【方案 §8.3】方案 A）。

    ⚠️ **不用 Langfuse Alerts**：实测告警没有公开 API（6 个候选端点全 404），
       「后端读告警状态」这条路走不通。改为后端拿指标自己比阈值。

    ⚠️ 返回形状与改造前**保持一致**（`{firing, pending, rules:[{name,state}]}`），
       前端告警卡不用改。

    ⚠️ 值为 None（无数据）时**不触发** —— 刚起服务、还没有流量是正常状态，
       误报会让人把「已接好、只是还没流量」当成故障。

    阈值取自 `observability.alert_thresholds`（Task 11 加进 app.yaml）。
    """
    thresholds = cfg("observability.alert_thresholds", {}) or {}
    node_p95_ms = float(thresholds.get("node_p95_ms", 10000))
    err_threshold = float(thresholds.get("http_5xx_rate", 0.05))

    rules: list[dict] = []

    # 规则一：按节点的 p95 越限（原 prometheus-rules.yml 的 RAGNodeP95High，
    # 那条因为指标名对不上而**从来没触发过**，【方案 §1.1】）
    for row in values.get("node_latency") or []:
        state = "firing" if row["p95"] > node_p95_ms else "inactive"
        rules.append({"name": f"RAGNodeP95High[{row['node']}]", "state": state})

    # 规则二：5xx 比例（原按 http_status_code 过滤；现靠 level=ERROR，
    # 因为实测 Metrics API **没有 HTTP 状态码维度**）
    rate = values.get("error_rate")
    rules.append({
        "name": "RAGHttp5xxRate",
        "state": ("firing" if rate is not None and rate > err_threshold
                  else "inactive"),
    })

    return {
        "firing": sum(1 for r in rules if r["state"] == "firing"),
        "pending": 0,          # 自算没有 pending 态（原 Prometheus 的 for: 5m 概念）
        "rules": rules,
    }


async def retrieval_metrics() -> dict:
    """运行指标（§4.4）—— 唯一读 Langfuse 的接口。

    ⚠️ 不可用时返回 `available: False`，**api 层仍返 HTTP 200**（M4-D5）。
    ⚠️ token 用**累计值**不用 rate：问答是稀疏事件，窗口两端数值常常相同，
       rate 会算成 0（实测踩过 —— 指标明明有 331，rate 报 0）。
    """
    if _lf_config() is None:
        return {"available": False, "status": "langfuse_unavailable"}

    hours = int(cfg("observability.metrics_window_hours", 24))

    lat = await _lf_query(_build_metrics_query(
        measures=[("latency", "p50"), ("latency", "p95")],
        dimensions=None, hours=hours))
    # 5xx 率：实测**没有 HTTP 状态码维度**，改用 level=ERROR（【方案 §8.3】）
    err = await _lf_query(_build_metrics_query(
        measures=[("count", "count")], dimensions=["level"], hours=hours))
    tok = await _lf_query(_build_metrics_query(
        measures=[("totalTokens", "sum"), ("totalCost", "sum")],
        dimensions=["usageType"], hours=hours))
    nodes = await _lf_query(_build_metrics_query(
        measures=[("latency", "p95")], dimensions=["name"], hours=hours))

    values = {
        "latency_p50": _extract_scalar(lat, "p50_latency"),
        "latency_p95": _extract_scalar(lat, "p95_latency"),
        "error_rate": _error_rate_from_levels(err),
        "token_usage": _sum_rows(tok, "sum_totalTokens"),
        "cost": _sum_rows(tok, "sum_totalCost"),
        "node_latency": _node_latency_rows(nodes),
    }

    payloads = {"lat": lat, "err": err, "tok": tok, "nodes": nodes}

    # ⚠️ 判据是「有没有一个查询真的答上了」，**不是**「有没有非空值」。
    #    刚起服务、还没人来提问时，窗口是空的：`_lf_query` 仍然返回了
    #    payload（`{"data":[...]}` 或 `{"data":[]}`），那说明**接好了、只是还没流量** ——
    #    这种情形必须是 available:true。把它显示成「暂不可用」会让人以为接错了，
    #    而改造前的代码专门写过注释避免这件事。
    if not any(p is not None for p in payloads.values()):
        return {"available": False, "status": "langfuse_unavailable"}

    return {"available": True, **values, "alerts": _evaluate_alerts(values)}
