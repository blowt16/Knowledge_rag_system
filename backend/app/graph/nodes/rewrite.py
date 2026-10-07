"""节点 5：rewrite —— 查询扩展（三类检索查询）（§3.5.3 节点 5）。

| source | 生成方式 | 派生 target | 权重 | 理由 |
|---|---|---|---|---|
| `verbatim` | **原样**使用 resolved_query | `both` | 1.0 | 保文号精度 ——「桂电教〔2025〕28号」一个字都不能改 |
| `keywords` | LLM 抽取关键词 | `bm25` | 1.0 | BM25 对短查询敏感，长句会被稀释 |
| `hyde` | LLM 生成假设答案 | `vector` | 1.0 | 补口语化问法 vs 正式文本的鸿沟 |

⚠️ **`target` 与 `weight` 由服务端派生，不由模型输出**：
   否则会出现「`source = keywords` 但 `target = vector`」这类**非法组合**
   （keywords 是给 BM25 用的短查询串，送去向量检索没有意义）；
   且权重一旦交给模型填，**无法保证一律 1.0** ——
   而权重恒 1.0 是 5.3 消融实验有效的前提。

⚠️ 解析失败 → 退化为**一条 `verbatim`** —— 这就是「标准 hybrid」，
   **不是另一个单独的分支**。

⚠️ 为什么不让 `resolve` 做这件事：resolve 跑在 Router **之前**，
   不知道后面走不走检索。若在此扩展，闲聊与澄清分支会白白多付一次 LLM 调用。
"""

from __future__ import annotations

import logging
import time

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.graph.state import RAGState, RetrievalQuery, NodeTrace
from app.eval import config as eval_config

logger = logging.getLogger(__name__)

# 服务端固定映射表 —— 模型不得输出 target / weight
TARGET_BY_SOURCE = {"verbatim": "both", "keywords": "bm25", "hyde": "vector"}
WEIGHT = 1.0



def _derive(source: str, text: str) -> RetrievalQuery:
    return RetrievalQuery(
        text=text,
        target=TARGET_BY_SOURCE.get(source, "both"),   # type: ignore[typeddict-item]
        weight=WEIGHT,
        source=source,                                  # type: ignore[typeddict-item]
    )


def fallback_queries(query: str) -> list[RetrievalQuery]:
    """解析失败时的标准 hybrid：一条 verbatim。"""
    return [_derive("verbatim", query)]


async def rewrite_node(state: RAGState) -> dict:
    started = time.perf_counter()
    query = state.get("resolved_query") or state.get("query", "")

    # 消融开关（§5.3 第 1–4 行：查询扩展全关）。
    # 线上 `eval_config` 为空 → `switches()` 返回 None → 不走这一支。
    sw = eval_config.switches(state)
    if sw is not None:
        want = {s for s in ("verbatim", "keywords", "hyde") if sw.get(f"expand_{s}")}
        if not want:
            # 一次 LLM 调用都不付，直接用原句 —— 这就是「不做查询扩展」的基线
            return {
                "retrieval_queries": [_derive("verbatim", query)],
                "trace": [NodeTrace(node="rewrite", ms=0, recalled=1, degraded=None)],
            }
    else:
        want = {"verbatim", "keywords", "hyde"}

    queries: list[RetrievalQuery] = []
    degraded: str | None = None
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": render("rewrite", query=query)}],
            timeout=float(cfg("timeouts.rewrite", 15)),
            max_tokens=1024,
        )
        if isinstance(data, dict):
            # 模型有时会包一层
            data = data.get("queries") or data.get("items") or []
        if isinstance(data, list):
            seen: set[str] = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source", "")).strip()
                text = str(item.get("text", "")).strip()
                if source in TARGET_BY_SOURCE and text and source not in seen:
                    # 消融：只保留被打开的查询类型（§5.3 第 5–7 行逐类叠加）
                    if source not in want:
                        continue
                    queries.append(_derive(source, text))
                    seen.add(source)
    except llm.LLMTimeout:
        logger.warning("查询扩展超时，退化为标准 hybrid",
                       extra={"event": "rewrite.timeout", "node": "rewrite"})
        degraded = "timeout"
    except Exception:  # noqa: BLE001
        logger.warning("查询扩展失败，退化为标准 hybrid",
                       extra={"event": "rewrite.fallback", "node": "rewrite"})
        degraded = "unavailable"

    if not queries:
        queries = fallback_queries(query)
        # 解析成功但一条可用查询都没解出来，同样是退化 —— 也要留痕
        degraded = degraded or "unavailable"

    # verbatim 必须存在且**逐字等于** resolved_query（保文号精度）
    if not any(q["source"] == "verbatim" for q in queries):
        queries.insert(0, _derive("verbatim", query))
    else:
        for q in queries:
            if q["source"] == "verbatim":
                q["text"] = query

    return {
        "retrieval_queries": queries,
        # ⚠️ 兜底必须留痕（评审 I2）：`app.cli eval-multiturn` 的护栏靠 degraded
        #    判断「这一轮指标还反不反映设计行为」，有降级就拒绝写评测文件。
        "trace": [NodeTrace(node="rewrite",
                            ms=int((time.perf_counter() - started) * 1000),
                            recalled=len(queries), degraded=degraded)],
    }
