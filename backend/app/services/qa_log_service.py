"""问答日志落库（§3.3.1 `qa_logs`）。

这张表是**评测与统计的原始数据**：拒答率、规则命中率、幻觉率、token 用量
全部从这里出。`trace_id` 建了索引 —— 它是「按 trace_id 还原单次请求全链路」
这条验收的落点（与日志、Jaeger 的关联键）。

⚠️ `retrieval_confidence` 并入 `node_timings` 的 JSON，**不新增独立列** ——
   它是单值且只用于观测，不像 `route_source` / `degraded` 那样要被 SQL 直接筛选聚合。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app import db

logger = logging.getLogger(__name__)


async def write_qa_log(
    *,
    session_id: str,
    user_id: str,
    user_role: str,
    trace_id: str,
    query: str,
    resolved_query: str,
    route: str,
    retrieved: list[str],
    reranked: list[str],
    answer: str,
    refused: bool,
    refusal_reason: str | None,
    verify_report: dict | None,
    route_source: str,
    degraded: bool,
    latency_ms: int,
    node_timings: list[dict[str, Any]],
) -> None:
    row_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO qa_logs
               (id, session_id, user_id, trace_id, user_role, question, resolved_query,
                route, retrieved_chunk_ids, reranked_chunk_ids, answer, is_refused,
                refusal_reason, verify_report, route_source, degraded, latency_ms,
                node_timings)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)""",
            row_id, session_id or None, user_id or None, trace_id or None,
            user_role or None, query, resolved_query or None, route or None,
            retrieved, reranked, answer,
            1 if refused else 0,
            refusal_reason if refusal_reason in ("no_candidate", "insufficient_evidence")
            else None,
            verify_report,
            route_source if route_source in ("rule", "llm") else None,
            1 if degraded else 0,
            latency_ms,
            node_timings,
        )
    return row_id
