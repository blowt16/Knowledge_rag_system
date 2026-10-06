"""节点 6：retrieve —— 混合检索（§3.5.3 节点 6）。

按 `target` 分发到两路，带 ACL / 版本过滤；**固定过采样只对向量路**（§3.3.4）。

**失败与降级**（原节点完全没有失败分支 —— 施工者会漏掉，
表现为「索引一坏就静默拒答」或直接 500）：

| 情形 | 处理 |
|---|---|
| **单路失败** | 记 `degradation_events`（`node=vector`/`bm25`、`kind=index_invalid`/`unavailable`），**继续用剩余那一路**，并在 `rerank_degraded` 上体现 |
| **两路都失败** | `candidates=[]` → 走 `refuse` 分支（与候选为空同路径） |

> **降级必须可见**：这条路走通了但少了一路，指标上会表现为 Recall 下降。
> 不记 `degradation_events` 的话，你会看到「检索质量突然变差」却找不到原因。
"""

from __future__ import annotations

import asyncio
import logging
import time as _time

from app import db
from app.core.config import cfg
from app.core.deps import UserContext
from app.graph.state import Chunk, NodeTrace, RAGState, RetrievalQuery
from app.retrieval import bm25, eval_config, vector
from app.retrieval.fusion import weighted_rrf
from app.retrieval.search import bm25_retrieve, vector_retrieve

logger = logging.getLogger(__name__)


async def _record_degradation(node: str, kind: str, detail: str,
                              session_id: str | None,
                              conn: "asyncpg.Connection | None" = None) -> None:
    """记降级事件。

    ⚠️ 传 `conn` 时**复用调用方的事务**，不要另开一个 ——
       在 `async with db.tx()` 内部再调 `db.tx()` 会从池里**另取一条连接**，
       并发下容易耗尽连接池；而且外层事务若回滚，这条降级记录却已独立提交。
       2026-10-05 实测见过一次 'Resetting connection with an active transaction'，
       虽只出现在图崩溃那次、此后未复现，但这个嵌套是真隐患。
    """
    async def _write(c) -> None:
        await c.execute(
            """INSERT INTO degradation_events (id, session_id, node, kind, detail)
               VALUES (md5(random()::text || clock_timestamp()::text),
                       $1, $2, $3, $4)""",
            session_id, node, kind, detail[:500],
        )
    try:
        if conn is not None:
            await _write(conn)
            return
        async with db.tx() as own_conn:
            await _write(own_conn)
    except Exception:  # noqa: BLE001 —— 记降级失败不该影响主链路
        logger.warning("写降级事件失败", extra={"event": "degradation.write_failed"})


async def retrieve_node(state: RAGState) -> dict:
    started = _time.perf_counter()
    queries: list[RetrievalQuery] = state.get("retrieval_queries") or []
    user_lite = state.get("user")
    session_id = state.get("session_id")
    include_restricted = bool(state.get("include_restricted"))
    degraded_kinds: list[str] = []

    if not queries:
        return _result([], started, ["no_query"], session_id)

    user = UserContext(id=user_lite.id, username="", role=user_lite.role,
                       token_version=0)

    # 可用性预检：向量库空 / BM25 索引缺失或版本失效 → 该路不可用
    vector_ok = True
    bm25_ok = bm25.is_available()

    async with db.tx() as conn:
        if not bm25_ok:
            degraded_kinds.append("index_invalid")
            await _record_degradation(
                "bm25", "index_invalid", "索引或映射表缺失/版本不匹配",
                session_id, conn)
        ranked: list[tuple[RetrievalQuery, list[Chunk]]] = []
        for query in queries:
            target = query.get("target", "both")
            # 消融开关（§5.3）：bm25 关掉时把这一路从 target 里摘掉。
            # 线上 eval_config 为空 → `on()` 恒返回 True，行为一字不变。
            if not eval_config.on(state, "bm25") and target == "both":
                target = "vector"
            elif not eval_config.on(state, "bm25") and target == "bm25":
                continue

            if target in ("vector", "both") and vector_ok:
                try:
                    hits = await vector_retrieve(
                        conn, query["text"], user,
                        include_restricted=include_restricted)
                    ranked.append((query, hits))
                except Exception as e:  # noqa: BLE001
                    vector_ok = False
                    degraded_kinds.append("unavailable")
                    await _record_degradation("vector", "unavailable", str(e), session_id, conn)

            if target in ("bm25", "both") and bm25_ok:
                try:
                    hits = await bm25_retrieve(
                        conn, query["text"], user,
                        include_restricted=include_restricted)
                    ranked.append((query, hits))
                except Exception as e:  # noqa: BLE001
                    bm25_ok = False
                    degraded_kinds.append("unavailable")
                    await _record_degradation("bm25", "unavailable", str(e), session_id, conn)

    # 两路都失败 → candidates 为空 → 走 refuse 分支
    if not vector_ok and not bm25_ok:
        return _result([], started, degraded_kinds, session_id, recalled=0)

    # 消融第 2 行（+BM25 但 RRF 未开）：文档没定怎么合，这里用 min-max 归一化相加。
    # ⚠️ 这是**消融脚手架**，不是产品语义（见 retrieval/eval_config.py 的说明）。
    candidates = (weighted_rrf(ranked) if eval_config.on(state, "rrf")
                  else _normalized_union(ranked))
    return _result(candidates, started, degraded_kinds, session_id,
                   recalled=len(candidates))


def _normalized_union(ranked: list[tuple[RetrievalQuery, list[Chunk]]]) -> list[Chunk]:
    """不做 RRF 的朴素融合：各路 min-max 归一化后相加，同 chunk 累加。

    单路时与它自己的分数顺序一致（归一化是单调变换）。
    """
    merged: dict[str, Chunk] = {}
    scores: dict[str, float] = {}
    for _query, hits in ranked:
        if not hits:
            continue
        vals = [c.score for c in hits]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        for c in hits:
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + (c.score - lo) / span
            merged.setdefault(c.chunk_id, c)
    for cid, chunk in merged.items():
        chunk.score = scores[cid]
    return sorted(merged.values(), key=lambda c: c.score, reverse=True)


def _result(candidates: list[Chunk], started: float, degraded_kinds: list[str],
            session_id: str | None, recalled: int = 0) -> dict:
    return {
        "candidates": candidates,
        "rerank_degraded": bool(degraded_kinds),
        "trace": [NodeTrace(node="retrieve",
                            ms=int((_time.perf_counter() - started) * 1000),
                            recalled=recalled,
                            degraded=degraded_kinds[0] if degraded_kinds else None)],
    }
