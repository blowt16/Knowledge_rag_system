"""图装配（§3.5.2）。

```
START
  ↓
resolve ──────────────── 指代消解 + 语义补全
  ↓
route ────────────────── 三分类（条件边）
  ├── chat     → chat 节点 ────→ END          ← 节点 3
  ├── clarify  → clarify 节点 ─→ END          ← 节点 4
  └── knowledge
        ↓
      rewrite ────────── 生成三类检索查询（1 次 LLM 调用）
        ↓
      retrieve ───────── 按 target 分发，多路并行 → 加权 RRF
        ↓
      rerank ─────────── Cross-Encoder 精排
        ├── 候选为空 → refuse → END
        └── 非空 → build_context → generate ─┬─ ANSWERED → cite → END
                                             └─ REFUSED  → END
```

**三个关键设计**（§3.5.2）：
1. **拒答的两道机制**：候选为空由 `rerank` 之后的条件边做**零成本短路**；
   充分性判定并入 `generate` 的单次结构化调用。
2. **图是无状态的**：`messages` 表是历史的**唯一权威**，每轮从库里加载
   （按 §3.8.3 的 token 预算），图作为无状态函数执行。
   **不使用 LangGraph 的 checkpointer** —— 移除 interrupt 后它已没有任何必要用途。
3. **resolve 可跳过**：命中无信息量词表、或无历史、或无代词且无省略特征时直接透传。

⚠️ `chat` 与 `clarify` 是**独立节点**，不是 `route` 内部的逻辑 ——
   两者各自有 LLM 调用与流式输出，SSE 的发出点也不同。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.build_context import build_context_node
from app.graph.nodes.chat import chat_node
from app.graph.nodes.cite import cite_node
from app.graph.nodes.clarify import clarify_node
from app.graph.nodes.generate import generate_node
from app.graph.nodes.refuse import refuse_node
from app.graph.nodes.rerank import rerank_node
from app.graph.nodes.resolve import resolve_node
from app.graph.nodes.retrieve import retrieve_node
from app.graph.nodes.rewrite import rewrite_node
from app.graph.nodes.route import route_node
from app.graph.state import RAGState


def _after_route(state: RAGState) -> Literal["chat", "clarify", "rewrite"]:
    route = state.get("route", "knowledge")
    if route == "chat":
        return "chat"
    if route == "clarify":
        return "clarify"
    return "rewrite"


def _after_rerank(state: RAGState) -> Literal["build_context", "refuse"]:
    """候选为空的**零成本短路** —— 全链路唯一的免模型拒答入口。"""
    return "build_context" if (state.get("reranked") or []) else "refuse"


def _after_generate(state: RAGState) -> Literal["cite", "__end__"]:
    """条件边读 ★`decision`。

    `REFUSED_NO_EVIDENCE` → END（服务端已用固定文案作答，不发 citations / verify）
    `ANSWERED` → cite
    """
    return "cite" if state.get("decision") == "ANSWERED" else END


def build_graph():
    graph = StateGraph(RAGState)

    graph.add_node("resolve", resolve_node)
    graph.add_node("route", route_node)
    graph.add_node("chat", chat_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("rewrite", rewrite_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("rerank", rerank_node)
    graph.add_node("refuse", refuse_node)
    graph.add_node("build_context", build_context_node)
    graph.add_node("generate", generate_node)
    graph.add_node("cite", cite_node)

    graph.add_edge(START, "resolve")
    graph.add_edge("resolve", "route")
    graph.add_conditional_edges("route", _after_route,
                                {"chat": "chat", "clarify": "clarify",
                                 "rewrite": "rewrite"})
    graph.add_edge("chat", END)
    graph.add_edge("clarify", END)
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("retrieve", "rerank")
    graph.add_conditional_edges("rerank", _after_rerank,
                                {"build_context": "build_context", "refuse": "refuse"})
    graph.add_edge("refuse", END)
    graph.add_edge("build_context", "generate")
    graph.add_conditional_edges("generate", _after_generate,
                                {"cite": "cite", END: END})
    graph.add_edge("cite", END)

    # ⚠️ 不使用 checkpointer：messages 表是历史的唯一权威
    return graph.compile()


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()
