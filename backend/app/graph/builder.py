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

from app.core import observability, trace

from app.graph.node_io import (
    log_node_details,
    node_input,
    node_log_fields,
    node_output,
)
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
    """给节点套一层 Langfuse 观测（【方案 §5.1】）。

    ⚠️⚠️ **metadata 属性里只放"多少钱、几秒、降没降级"，不放正文**（【方案 §7】）。
       正文只经 `node_io` 的摘要通道进 input/output，且脱敏钩子照样生效。

    ⚠️ **input/output 只放摘要**（`node_io.py`）：query、chunk_id / 文档名 /
       页码 / 分数、正文前 80 字。**不把整个 state 传上去** —— 完整检索正文
       不上云；脱敏钩子对这些字符串同样生效。

    ⚠️ **必须关掉 SDK 的自动内容捕获**（`@observe` 装饰器那边叫
       `capture_input=False, capture_output=False`）：
       Langfuse 默认会把输入输出与异常都记下来（官方：automatically capture
       inputs, outputs, timings, and errors）。不关掉就等于把 state 整个传上去。
       ⚠️ 本 SDK 版本（4.17.0）的 `start_as_current_observation` **没有**这对
       参数（实测签名），这里等价的做法是**显式传 `input=None, output=None`** ——
       别照着上面那行去加参数，会 TypeError。
       现在 input 由 `node_io.node_input()` 显式给摘要；output 执行完再填。

    ⚠️ 异常**只记类型、不记 message**：message 可能回显 prompt。
       实测过「注释声称安全 ≠ 实际安全」的教训（旧 builder.py:93-97），
       `test_langfuse_node_span.py` 是这条承诺每次都跑的凭据。

    ★★ **异常不能从 `with` 里逃出去**（Task 5 实施时实测，方案原稿未覆盖）：
       Langfuse 内部走 `otel_tracer.start_as_current_span(...)`（`client.py`
       `_start_as_current_otel_span_with_processed_media`），而 OTel 的
       `use_span` 缺省 `record_exception=True`；SDK 的
       `start_as_current_observation` **没有**任何参数能关掉它（实测签名里
       没有 `capture_input`/`capture_output`/`record_exception`）。
       异常一旦从 with 逃出，OTel 就把 `exception.message`（= `str(e)`，
       可能回显 prompt）与整条栈写成 span **事件** —— 实测标记文本原样出现在
       发出的字节里（`test_node_exception_records_type_only` 当场证伪）。
       所以：with 内部**先吞下异常**、记完类型，出了 with 再抛。
    """
    @functools.wraps(fn)
    async def _wrapper(state: RAGState) -> dict:
        client = observability.get_client()

        with observability.node_context(node_name):
            with trace.trace_span(f"node.{node_name}", layer="node") as stage:
                if client is None:
                    # 未启用（跑测试/CLI）：不产生云端观测，但本地日志照常
                    result = await fn(state)
                else:
                    failure: BaseException | None = None
                    with client.start_as_current_observation(
                        as_type="span", name=f"node.{node_name}",
                        input=node_input(node_name, state), output=None,
                    ) as obs:
                        try:
                            result = await fn(state)
                        except Exception as e:  # noqa: BLE001
                            obs.update(level="ERROR",
                                       status_message=type(e).__name__,
                                       metadata={"rag.error": type(e).__name__})
                            failure = e   # ★ 见上面那条：出了 with 再抛，别在这里 raise
                        else:
                            if isinstance(result, dict):
                                out = node_output(node_name, result)
                                entries = result.get("trace") or []
                                if entries:
                                    last = entries[-1]
                                    meta = {
                                        "rag.duration_ms": int(last.get("ms", 0)),
                                        "rag.recalled": int(last.get("recalled", 0)),
                                    }
                                    degraded = last.get("degraded")
                                    if degraded:
                                        meta["rag.degraded"] = str(degraded)
                                    obs.update(metadata=meta, output=out)
                                else:
                                    obs.update(output=out)

                    if failure is not None:
                        raise failure

                if isinstance(result, dict):
                    stage.set(**node_log_fields(node_name, result, state))

            # 节点 summary 已由 trace_span 打出；DEBUG 详情排在它后面、缩进一级
            if isinstance(result, dict):
                log_node_details(node_name, result, state, depth=stage.depth + 1)
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
