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
from datetime import date, datetime, time, timedelta

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


# ⚠️ 指标名**按实测写**（M4 那版是占位，两处都不对）。
#    2026-10-06 起 spanmetrics 后从 Prometheus 的
#    `/api/v1/label/__name__/values` 直读，实际是：
#        spanmetrics_calls_total
#        spanmetrics_duration_milliseconds_{bucket,sum,count}
#    占位那版写的是 `span_metrics_latency_bucket` —— 名字不对、单位也不是 ms。
#    `duration_milliseconds_*` 的直方图单位就是**毫秒**，与前端「延迟 p50（ms）」对得上。
_PROM_QUERIES = {
    "latency_p50": ('histogram_quantile(0.50, '
                    'sum(rate(spanmetrics_duration_milliseconds_bucket[5m])) by (le))'),
    "latency_p95": ('histogram_quantile(0.95, '
                    'sum(rate(spanmetrics_duration_milliseconds_bucket[5m])) by (le))'),
    # HTTP 5xx 占比。http.status_code 是应用在 HTTP span 上打的属性，
    # 已在 collector 里登记为 spanmetrics 维度（值为空串的其它 span 用 != "" 排除）。
    # ⚠️ 分子要 `or vector(0)`：一次 5xx 都没有时该序列**不存在**，
    #    整条表达式会算成「无结果」→ 接口返回 null → 面板显示「—」，
    #    看起来像没接上。补 0 之后是 0/总量 = 0，语义正确。
    "error_rate": ('(sum(rate(spanmetrics_calls_total{http_status_code=~"5.."}[5m]))'
                   ' or vector(0))'
                   ' / clamp_min(sum(rate(spanmetrics_calls_total'
                   '{http_status_code!=""}[5m])), 0.001)'),
    # 应用自己发的计数器（collector 的 metrics 管道接了 otlp 接收器才有）。
    # ⚠️ 用**累计值**而不是 `rate(...)`：问答是稀疏事件，窗口两端数值常常相同，
    #    rate 会算成 0（实测踩过 —— 指标明明有 331，rate 报 0），
    #    在「token 用量」这种展示型面板上会让人以为没接上。
    "token_usage": 'sum(llm_tokens_total)',
}


async def _prom_rules() -> dict | None:
    """读 Prometheus 的**告警规则状态**（§4.4 状态卡：有 firing 即标红）。

    ⚠️ 走 Prometheus 自己的 `/api/v1/rules`，**不接 Alertmanager**（§3.2.3.3）：
       Alertmanager 解决的是通知的分组/去重/静默/分发，而本项目无人值班、
       没有接收方。Prometheus 自己就把「当前是否越限」算好了，读出来展示即可。
    """
    if not PROMETHEUS_URL:
        return None
    try:
        async with httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=_PROM_TIMEOUT,
                                     transport=_prom_transport) as client:
            resp = await client.get("/api/v1/rules", params={"type": "alert"})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:  # noqa: BLE001 —— 告警读不到不该让页面挂掉
        logger.warning("Prometheus 规则查询失败：%s", e,
                       extra={"event": "stats.prom_rules_failed"})
        return None

    rules: list[dict] = []
    for group in (payload.get("data") or {}).get("groups") or []:
        for rule in group.get("rules") or []:
            rules.append({"name": rule.get("name", ""), "state": rule.get("state", "")})
    return {
        "firing": sum(1 for r in rules if r["state"] == "firing"),
        "pending": sum(1 for r in rules if r["state"] == "pending"),
        "rules": rules,
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

    alerts = await _prom_rules()

    # ⚠️ 加 `alerts is None` 这一支：Prometheus 起来了、规则也读到了，但 span
    #    还没产生过（刚起服务、还没人来提问）时会「四条 query 全空」——
    #    那种情况仍算**可用**，否则仪表盘会把「已接好、只是还没流量」
    #    显示成「暂不可用」，让人以为接错了。
    if all(v is None for v in values.values()) and alerts is None:
        return {"available": False, "status": "prometheus_unavailable"}

    return {"available": True, **values, "alerts": alerts}
