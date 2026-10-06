"""管理端统计（§3.7.3 / §4.4 仪表盘）。

**两块数据源，别混**（§4.4）：
- **业务指标**读 PostgreSQL（本模块的 overview / trend / refusals / hot-questions）；
- **运行指标**用 PromQL 查 Prometheus（只有 `retrieval_metrics`）。

⚠️ **`refusal_rate` 的口径是 M4-D2 定的**（计划文档 §9.0）：分母是
   **`route='knowledge'` 的轮次**。§3.7.3 说「定义与分母见 5.2」，而 §5.2 里
   **只有评测集的漏答率/误答率** —— 文档缺口，口径落在这里。
   闲聊/澄清轮次没有证据面，计入分母会把拒答率稀释成没有意义的偏低值。

⚠️ **`qa_count` 不含评测轮次**：评测结果落 `eval_case_results`，天然不进
   业务口径。**若 M5 让评测也写 `qa_logs`，必须在这里加过滤**（已写进 §9.0）。

⚠️ **M4 没有 Prometheus**（三个容器在 M5 才起）：`PROMETHEUS_URL` 为空时
   `retrieval_metrics` 返回 `{available: false, status: "prometheus_unavailable"}`，
   由 api 层照常返 **HTTP 200** —— §4.4 要求运行指标不可用时
   **业务指标区块照常渲染**，返 500 会把整个仪表盘打白。
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import httpx

from app import db
from app.core.config import cfg

logger = logging.getLogger(__name__)

# Prometheus 基址。M5 起三个容器后由配置或环境变量给上；M4 为空 → 走不可用分支。
PROMETHEUS_URL: str = (cfg("observability.prometheus_url", "")
                       or os.environ.get("PROMETHEUS_URL", ""))
# 测试注入用（httpx.MockTransport）—— 让「Prometheus 答得上」这条分支
# 走**真实的 HTTP 代码路径**，而不是把内部函数打桩
_prom_transport: httpx.BaseTransport | None = None

# 超时要短：仪表盘是给人看的，Prometheus 慢不该拖垮页面
_PROM_TIMEOUT = 3.0

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
    """
    async with db.tx() as conn:
        rows = await conn.fetch(
            """SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS d,
                      count(*) AS qa_count,
                      count(*) FILTER (WHERE is_refused = 1) AS refusal_count
                 FROM qa_logs
                WHERE created_at >= date_trunc('day', now())
                                    - ($1 - 1) * interval '1 day'
                GROUP BY d""",
            days)
    by_day = {r["d"]: r for r in rows}

    today = date.today()
    out = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        hit = by_day.get(key)
        out.append({
            "date": key,
            "qa_count": hit["qa_count"] if hit else 0,
            "refusal_count": hit["refusal_count"] if hit else 0,
        })
    return {"days": out}


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


async def _prom_query(query: str) -> float | None:
    """查一条 PromQL，取第一个标量。任何失败都返回 None（**由调用方决定怎么降级**）。"""
    if not PROMETHEUS_URL:
        return None
    try:
        async with httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=_PROM_TIMEOUT,
                                     transport=_prom_transport) as client:
            resp = await client.get("/api/v1/query", params={"query": query})
            resp.raise_for_status()
            payload = resp.json()
        result = (payload.get("data") or {}).get("result") or []
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception as e:  # noqa: BLE001 —— 运行指标缺失不该让页面挂掉
        logger.warning("Prometheus 查询失败：%s", e,
                       extra={"event": "stats.prom_failed"})
        return None


# ⚠️ 这几条 PromQL 是**占位口径**：M5 起 OTel Collector 的 spanmetrics 之后，
#    真正的指标名与标签以那时的实测为准，届时改这里即可（调用方不变）。
_PROM_QUERIES = {
    "latency_p50": 'histogram_quantile(0.50, sum(rate(span_metrics_latency_bucket[5m])) by (le))',
    "latency_p95": 'histogram_quantile(0.95, sum(rate(span_metrics_latency_bucket[5m])) by (le))',
    "error_rate": 'sum(rate(http_requests_total{status=~"5.."}[5m]))',
    "token_usage": 'sum(rate(llm_tokens_total[5m]))',
}


async def retrieval_metrics() -> dict:
    """运行指标（§4.4）—— 唯一读 Prometheus 的接口。

    ⚠️ 不可用时返回 `available: False`，**api 层仍返 HTTP 200**（M4-D5）。
    """
    if not PROMETHEUS_URL:
        return {"available": False, "status": "prometheus_unavailable"}

    values = {}
    for key, query in _PROM_QUERIES.items():
        values[key] = await _prom_query(query)

    if all(v is None for v in values.values()):
        # 地址配了但一条都答不上来（Prometheus 没起 / 指标名还没对上）
        return {"available": False, "status": "prometheus_unavailable"}

    return {"available": True, **values}
