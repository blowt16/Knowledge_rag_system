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
from app.core.exceptions import AppError, NotFound

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


# ============================================================
# 管理端：拒答明细与标注（§4.3.1.3）
# ============================================================

def _annotation(row) -> dict | None:
    """把左连接的标注列组装成对象；没标注时返回 None（不是空对象）。"""
    if row["ann_qa_log_id"] is None:
        return None
    return {
        "suggested_document_id": row["suggested_document_id"],
        "note": row["note"],
        "annotated_by": row["annotated_by"],
        "updated_at": (row["ann_updated_at"].isoformat()
                       if row["ann_updated_at"] else None),
    }


async def list_refusals(*, page: int = 1, page_size: int = 20) -> dict:
    """拒答明细（§4.3.1.3）—— 只列 `is_refused = 1` 的轮次。

    这张清单就是「学生问了但知识库答不上来」的清单，可反哺知识库补充文档。
    """
    async with db.tx() as conn:
        total = await conn.fetchval("SELECT count(*) FROM qa_logs WHERE is_refused = 1")
        rows = await conn.fetch(
            """SELECT q.id, q.question, q.refusal_reason, q.created_at,
                      a.qa_log_id AS ann_qa_log_id, a.suggested_document_id,
                      a.note, a.annotated_by, a.updated_at AS ann_updated_at
                 FROM qa_logs q
                 LEFT JOIN refusal_annotations a ON a.qa_log_id = q.id
                WHERE q.is_refused = 1
                ORDER BY q.created_at DESC
                OFFSET $1 LIMIT $2""",
            (page - 1) * page_size, page_size)
    return {
        "items": [{
            "id": r["id"],
            "question": r["question"] or "",
            "refusal_reason": r["refusal_reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "annotation": _annotation(r),
        } for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    }


async def annotate_refusal(log_id: str, *, suggested_document_id: str | None = None,
                           note: str | None = None, annotated_by: str) -> dict:
    """写标注（§4.3.1.3）。落在 `refusal_annotations`。

    ⚠️ **一条拒答记录只保留最新一次标注**（`qa_log_id` 上有 UNIQUE）——
       标注是可反复修改的运维动作，与「只增」的日志生命周期不同。
       所以这里是 upsert，不是 insert。

    ⚠️ **只覆盖本次给了的字段**（`COALESCE`）：管理员先标了「建议补充某文档」、
       过两天再补一句备注，不该把文档建议抹掉。
    """
    async with db.tx() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM qa_logs WHERE id = $1 AND is_refused = 1", log_id)
        if not exists:
            raise NotFound("拒答记录不存在")

        if suggested_document_id is not None:
            doc = await conn.fetchval("SELECT 1 FROM documents WHERE id = $1",
                                      suggested_document_id)
            if not doc:
                # 不拦的话会撞外键 → 500，管理员看不出是自己填错了 id
                raise AppError("建议补充的文档不存在")

        row = await conn.fetchrow(
            """INSERT INTO refusal_annotations
                   (id, qa_log_id, suggested_document_id, note, annotated_by)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (qa_log_id) DO UPDATE SET
                   suggested_document_id = COALESCE(
                       EXCLUDED.suggested_document_id,
                       refusal_annotations.suggested_document_id),
                   note = COALESCE(EXCLUDED.note, refusal_annotations.note),
                   annotated_by = EXCLUDED.annotated_by,
                   updated_at = now()
               RETURNING qa_log_id, suggested_document_id, note, annotated_by, updated_at""",
            uuid.uuid4().hex, log_id, suggested_document_id, note, annotated_by)

    return {
        "suggested_document_id": row["suggested_document_id"],
        "note": row["note"],
        "annotated_by": row["annotated_by"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
