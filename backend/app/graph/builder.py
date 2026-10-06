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

import functools
from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph
from opentelemetry.trace import Status, StatusCode

from app.core import telemetry

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


def traced(node_name: str, fn):
    """给节点套一层 span（§3.2.3.3 加分项⑤：Jaeger 里能看见完整图执行轨迹）。

    ⚠️⚠️ **属性里只放"多少钱、几秒、降没降级"，一个字的正文都不放**（§3.2.3.2）。
       节点的输入输出全是用户提问与检索片段 —— 一旦当属性写进去，
       日志那边脱敏做得再好也没用，Jaeger 里照样摆着学号姓名。

    ⚠️⚠️ 异常**只记类型、不记 message**：`record_exception` 会把异常文本与栈写进
       span **事件**，而 LLM 相关异常的 message 可能回显 prompt。

       ★ **显式传 `record_exception=False` 是必须的**：不写它，
       `start_as_current_span` 的上下文管理器会**自动**记录异常 ——
       我第一版就是在注释里写了「刻意不用 record_exception」却没传这个参数，
       被 `test_span_redaction.py` 当场证伪（标记文本原样出现在 span 事件里）。
       注释声称的安全 ≠ 实际的安全，这条测试就是两者的对账。

    ⚠️ 用**手工埋点**而不是 `opentelemetry-instrumentation-langchain`：自动埋点
       默认就把 prompt 与模型返回写进 span 属性，得反过来去关（且不同版本开关名
       不一样，关了没关要靠翻 Jaeger 才知道）。这里从头到尾就没让它进过 span。
    """
    @functools.wraps(fn)
    async def _wrapper(state: RAGState) -> dict:
        tracer = telemetry.get_tracer("graph")
        with tracer.start_as_current_span(
            f"node.{node_name}",
            record_exception=False,          # ★ 见上面那条警告，别删
            set_status_on_exception=False,   # 状态由下面显式设置，不让 SDK 覆盖成 UNSET
        ) as span:
            span.set_attribute("rag.node", node_name)
            try:
                result = await fn(state)
            except Exception as e:  # noqa: BLE001
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("rag.error", type(e).__name__)
                raise
            if isinstance(result, dict):
                entries = result.get("trace") or []
                if entries:
                    last = entries[-1]
                    span.set_attribute("rag.duration_ms", int(last.get("ms", 0)))
                    span.set_attribute("rag.recalled", int(last.get("recalled", 0)))
                    degraded = last.get("degraded")
                    if degraded:
                        span.set_attribute("rag.degraded", str(degraded))
            return result
    return _wrapper


def build_graph():
    graph = StateGraph(RAGState)

    graph.add_node("resolve", traced("resolve", resolve_node))
    graph.add_node("route", traced("route", route_node))
    graph.add_node("chat", traced("chat", chat_node))
    graph.add_node("clarify", traced("clarify", clarify_node))
    graph.add_node("rewrite", traced("rewrite", rewrite_node))
    graph.add_node("retrieve", traced("retrieve", retrieve_node))
    graph.add_node("rerank", traced("rerank", rerank_node))
    graph.add_node("refuse", traced("refuse", refuse_node))
    graph.add_node("build_context", traced("build_context", build_context_node))
    graph.add_node("generate", traced("generate", generate_node))
    graph.add_node("cite", traced("cite", cite_node))

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
