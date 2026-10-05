"""节点 7：rerank —— 交叉编码器精排（§3.5.3 节点 7）。

**本节点负责「候选为空」的短路** —— 判定后走条件边到 `refuse`。
这是全链路**唯一的零成本拒答入口**（不调模型）。

`retrieval_confidence` 的落库出口：并入 `qa_logs.node_timings` 的 JSON
（如 `{"rerank": {"ms": 320, "confidence": 0.87}}`），**不新增独立列** ——
它是单值且只用于观测，不像 `route_source` / `degraded` 那样要被 SQL 直接筛选聚合。
"""

from __future__ import annotations

import time

from app.graph.state import Chunk, NodeTrace, RAGState
from app.retrieval.reranker import rerank


async def rerank_node(state: RAGState) -> dict:
    started = time.perf_counter()
    candidates: list[Chunk] = state.get("candidates") or []
    query = state.get("resolved_query") or state.get("query", "")

    if not candidates:
        # 零成本短路：不调模型、不调 GPU
        return {
            "reranked": [],
            "evidence": [],
            "retrieval_confidence": None,
            "trace": [NodeTrace(node="rerank", ms=0, recalled=0, degraded=None)],
        }

    result = await rerank(query, candidates)

    return {
        "reranked": result.chunks,
        # 降级时必须为 None —— 不能填 RRF 分（量纲不同）
        "retrieval_confidence": result.confidence,
        "rerank_degraded": bool(state.get("rerank_degraded")) or result.degraded,
        "trace": [NodeTrace(
            node="rerank",
            ms=int((time.perf_counter() - started) * 1000),
            recalled=len(result.chunks),
            degraded=result.degraded_kind,
        )],
    }
