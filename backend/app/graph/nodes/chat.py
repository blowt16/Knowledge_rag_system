"""节点 3：chat —— 普通问候（§3.5.3 节点 3）。

极简节点。**不检索、不引用、不加"📚参考来源"**。

⚠️ `chat` 与 `clarify` 是**独立节点，不是 `route` 内部的逻辑**。
   原拓扑把它们画成「`route` 直接到 `END`」的边，读者会理解成
   路由节点自己就把回复生成了 —— 而两者各自有 LLM 调用与流式输出。

输出契约（§3.5.2）：

| 节点 | 写到 state | 发出的 SSE 事件 |
|---|---|---|
| `chat` | `answer` | `token`（流式）；**不发** `citations` / `verify` / `decision` |

⚠️ 即 **只有 `knowledge` 这条路径才发 `citations` / `verify`**；另两条不发。
"""

from __future__ import annotations

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.graph.state import RAGState


FALLBACK = "你好，我是校园规章制度问答助手，可以帮你查询学籍、考试、转专业等相关规定。"


async def chat_node(state: RAGState) -> dict:
    import time

    from app.graph.state import NodeTrace

    started = time.perf_counter()
    query = state.get("resolved_query") or state.get("query", "")

    def _with_trace(out: dict) -> dict:
        out["trace"] = [NodeTrace(node="chat",
                                  ms=int((time.perf_counter() - started) * 1000),
                                  recalled=0, degraded=None)]
        return out

    timeout = float(cfg("timeouts.generate_ttft", 60))
    messages = [{"role": "user", "content": render("chat", query=query)}]

    # 有 stream writer 时边收边发 —— chat 也是流式输出（§3.5.2 的输出契约）
    writer = _stream_writer()
    if writer is not None:
        pieces: list[str] = []
        try:
            async for piece in llm.stream_raw(messages, timeout=timeout, max_tokens=256):
                pieces.append(piece)
                # 闲聊是纯文本，直接透传，不需要 JSON 解析
                writer({"type": "token", "text": piece})
            answer = "".join(pieces).strip()
            if answer:
                return _with_trace({"answer": answer, "decision": "ANSWERED"})
        except Exception:  # noqa: BLE001 —— 闲聊不该因为 LLM 抖动而失败
            pass
        writer({"type": "token", "text": FALLBACK})
        return _with_trace({"answer": FALLBACK, "decision": "ANSWERED"})

    try:
        answer = await llm.complete(messages, timeout=timeout, max_tokens=256)
        answer = (answer or "").strip() or FALLBACK
    except Exception:  # noqa: BLE001
        answer = FALLBACK

    return _with_trace({"answer": answer, "decision": "ANSWERED"})


def _stream_writer():
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:  # noqa: BLE001
        return None
