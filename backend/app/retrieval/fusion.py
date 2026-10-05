"""加权 RRF 融合（§3.5.3 节点 6）。

    score(d) = Σ   w_i / (k + rank_i(d))
            i ∈ 所有 (查询 × 检索器) 组合

`w_i` 一律取 **1.0**（暂不做权重调优）；`k = 60`（可配置，消融实验的调参对象）。

⚠️ 权重必须一律 1.0：若各类权重不等，消融表里 verbatim / keywords / hyde
   三行的增益会混入权重差异，**证明不了单个查询类型的价值**。

⚠️ 融合**去重键是 `chunk_id`**。同一 chunk 被多路命中时分数自然累加 ——
   这正是多查询的价值所在。
"""

from __future__ import annotations

from app.core.config import cfg
from app.graph.state import Chunk, RetrievalQuery


def weighted_rrf(
    ranked_lists: list[tuple[RetrievalQuery, list[Chunk]]],
    *,
    k: int | None = None,
    limit: int | None = None,
) -> list[Chunk]:
    """融合多路结果。

    `ranked_lists` 每项是 (该路的查询, 按序的结果)。
    返回按融合分降序、去重后的候选。
    """
    k = k if k is not None else int(cfg("retrieval.rrf_k", 60))
    limit = limit if limit is not None else int(cfg("retrieval.fusion_limit", 40))

    scores: dict[str, float] = {}
    best: dict[str, Chunk] = {}

    for query, chunks in ranked_lists:
        weight = float(query.get("weight", 1.0))
        for rank, chunk in enumerate(chunks, start=1):
            key = chunk.chunk_id
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            # 保留信息最全的那份（同一 chunk 来自不同路时 metadata 相同，
            # 但向量路带 text、BM25 路可能不带）
            existing = best.get(key)
            if existing is None or (not existing.text and chunk.text):
                best[key] = chunk

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]

    out: list[Chunk] = []
    for chunk_id, score in ordered:
        chunk = best[chunk_id]
        chunk.score = score
        out.append(chunk)
    return out


def fold_versions(
    candidates: list[Chunk], latest_by_group: dict[str, int]
) -> list[Chunk]:
    """版本折叠：丢弃 version ≠ 组内最大值的 chunk。

    ⚠️ `latest_by_group` **必须由调用方回查 PostgreSQL 得到**（§3.3.1），
       **不能在召回集内取最大**。

       若现行版 v5 的措辞与 query 不相似、而已废止的 v4 相似，Top-N 里只有 v4 ——
       在召回集内折叠会把 **v4 当成现行版本**返回，用户拿到已废止的政策，
       界面还按「当前生效」展示。**这会让亮点① 的版本隔离彻底失效。**

    ⚠️ 折叠的产物是**候选池**，不是最终结果。完整链路固定为：
       召回 → 版本折叠 → RRF 融合 → 精排 → Top-5 进 build_context。

    ⚠️ 折叠作用于**两路候选的并集**，不是只作用于向量那一路：
       BM25 对措辞雷同的旧版命中率天然更高，若它不做折叠，
       旧版会以高 RRF 分进入候选并可能被返回。
    """
    out: list[Chunk] = []
    for chunk in candidates:
        latest = latest_by_group.get(chunk.doc_group_id)
        if latest is None or chunk.version == latest:
            out.append(chunk)
    return out
