"""节点 4：clarify —— 澄清反问（§3.5.3 节点 4）。

**节点 4 是唯一没有兜底描述的 LLM 节点** —— 而按本节自己的原则
「**宁可多答不可多问**」，**它恰恰是最不该出错的那个**。

解析失败的兜底（**必须实现**）：

| 情形 | 兜底 |
|---|---|
| JSON 解析失败 / 超时 | 退化为**一条不带 `facets` 的固定澄清问句**：「你想问的是哪一项？可以补充一下具体场景。」—— **仍然走 clarify 分支**，只是没有可点选项 |
| `facets` 为空数组 | 同上 |
| 兜底也失败 | 归入 `knowledge` 分支 |

**为什么不直接归 `knowledge`**：走到 clarify 说明规则层或 LLM 已经判定
「这个问题问不清楚」，直接去检索大概率也是答非所问。**先问一句**比"猜着答"更符合本节的原则。

⚠️ **字段名在三个层面不同，别混**：模型输出的 JSON 字段叫 **`facets`**；
   写进状态与 SSE 载荷时叫 **`clarify_facets`**。
   前端读的是后者 —— **写成 `evt.facets` 会恒为 `undefined`，澄清选项永远渲染不出来**。

**恢复机制：重新进图，不用 `interrupt()`** —— 澄清不需要保留中间状态；
用户点 facet 或作答就是一次全新的请求，历史里已包含「反问」和「回答」两轮。

⚠️ 触发边界**仅两种**（详见节点 2）：指代无法消解、问题过短且无法定位主题。
   **该直接回答时反问用户，是体验最差的失败模式。**
"""

from __future__ import annotations

import logging

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.graph.state import RAGState

logger = logging.getLogger(__name__)

FALLBACK_QUESTION = "你想问的是哪一项？可以补充一下具体场景。"
FALLBACK_FACETS: list[str] = []



async def clarify_node(state: RAGState) -> dict:
    import time

    from app.graph.state import NodeTrace

    started = time.perf_counter()
    query = state.get("resolved_query") or state.get("query", "")

    def _with_trace(out: dict) -> dict:
        # ⚠️ 每个节点都必须写 trace —— 漏了不只是少一条耗时记录，
        #    还会让 SSE 那边 `_node_ran(final, "clarify")` 恒为 False，
        #    导致 clarify 的 route 事件（含 facets）永远不发。
        out["trace"] = [NodeTrace(
            node="clarify", ms=int((time.perf_counter() - started) * 1000),
            recalled=len(out.get("clarify_facets") or []), degraded=None)]
        return out

    writer = _stream_writer()
    if writer is not None:
        streamed = await _stream_clarify(state, query, writer)
        if streamed is not None:
            return _with_trace(streamed)

    try:
        data = await llm.complete_json(
            [{"role": "user", "content": render("clarify", history=_format_history(state.get("history") or []),
                query=query)}],
            timeout=float(cfg("timeouts.rewrite", 15)),
            max_tokens=512,
        )
        facets = data.get("facets") if isinstance(data, dict) else None
        question = str(data.get("question") or "").strip() if isinstance(data, dict) else ""

        if isinstance(facets, list):
            facets = [str(f).strip() for f in facets if str(f).strip()][:4]
        else:
            facets = []

        if not question:
            question = FALLBACK_QUESTION
            facets = list(FALLBACK_FACETS)

        return _with_trace({
            "clarify_question": question,
            # ⚠️ 写入 state 用 clarify_facets（SSE 与前端读的是这个名字）
            "clarify_facets": facets,
            "answer": question,
            "decision": "ANSWERED",
        })

    except Exception:  # noqa: BLE001
        logger.warning("澄清生成失败，退化为固定问句（仍走 clarify 分支）",
                       extra={"event": "clarify.fallback", "node": "clarify"})
        return _with_trace({
            "clarify_question": FALLBACK_QUESTION,
            "clarify_facets": list(FALLBACK_FACETS),
            "answer": FALLBACK_QUESTION,
            "decision": "ANSWERED",
        })


def _stream_writer():
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:  # noqa: BLE001
        return None


async def _stream_clarify(state: RAGState, query: str, writer):
    """复用节点 9 的流式解析器，**只把字段名换成 `question`**（§3.5.3 节点 4）。

    `facets` 是数组、不流式 —— 它随 `route` 事件整体下发（走 state，不走 writer）。
    """
    from app.graph.nodes.generate import StreamJsonParser

    parser = StreamJsonParser(field="question", require_decision=False)
    prompt = render("clarify", history=_format_history(state.get("history") or []),
                            query=query)
    try:
        async for piece in llm.stream_raw(
            [{"role": "user", "content": prompt}],
            timeout=float(cfg("timeouts.rewrite", 15)),
            max_tokens=512,
        ):
            for text in parser.feed(piece):
                if text:
                    writer({"type": "token", "text": text})
    except Exception:  # noqa: BLE001 —— 走下面的非流式兜底
        return None

    question = parser.answer_text.strip()
    if not question:
        return None

    # facets 从完整 JSON 里取（不流式）
    facets: list[str] = []
    try:
        import json
        data = json.loads(parser._raw)
        if isinstance(data.get("facets"), list):
            facets = [str(f).strip() for f in data["facets"] if str(f).strip()][:4]
    except Exception:  # noqa: BLE001
        facets = []

    return {
        "clarify_question": question,
        "clarify_facets": facets,
        "answer": question,
        "decision": "ANSWERED",
    }


def _format_history(history) -> str:
    if not history:
        return "（无）"
    lines = []
    for msg in history[-6:]:
        role = "用户" if getattr(msg, "role", "") == "user" else "助手"
        lines.append(f"{role}：{getattr(msg, 'content', '')}")
    return "\n".join(lines)
