"""问答编排（§3.2.4 / §3.7.2）。

**分层**（方案 §3.1）：`api/chat.py` 只做参数校验与响应封装；
会话锁、图执行、SSE 事件组装、落库这些**业务**都在这里。

⚠️ 本模块**不 import Starlette/FastAPI** —— 它产出的是 SSE 文本帧，
断连检测由调用方以回调传入。这样服务层可以脱离 HTTP 单独测试。

⚠️ **SSE 不能用 `EventSource`，必须用 `fetch` + `ReadableStream`**（口径钉死）：
   浏览器原生 `EventSource` **只能发 GET、且无法设置请求头** ——
   带不上 `Authorization: Bearer <JWT>`，只能 401，或被逼把 token 塞进查询串
   （token 会进入访问日志与浏览器历史，与「身份只能来自 JWT」冲突）。

⚠️ **两条拒答路径在 SSE 上如何区分**（钉死）：

| 拒答来源 | `decision` 事件 | `refused` 事件 |
|---|---|---|
| 候选为空（`rerank` 后短路 → 节点 11） | **不发送** | **发送**，`reason="no_candidate"` |
| 模型判定证据不足（节点 9 → 直接 END） | **发送** `REFUSED_NO_EVIDENCE` | **发送**，`reason="insufficient_evidence"` |

⚠️ `error` 是**终止事件**：发完 `error` 后**不再发 `done`**；
   前端收到 `error` 即停止 loading，并**保留已流出的正文**（不整条清空）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable

from app import db
from app.core.config import cfg
from app.core.deps import UserContext
from app.core.telemetry import current_trace_id
from app.graph.builder import get_graph
from app.graph.state import UserContextLite, new_state
from app.services.conversation_service import (
    append_messages,
    create_conversation,
    touch_conversation,
)
from app.services import context_service
from app.services.qa_log_service import write_qa_log

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 缓冲，否则流式会被攒着一次性下发
    "X-Accel-Buffering": "no",
}

DisconnectCheck = Callable[[], Awaitable[bool]]


def sse(event: str, data: dict) -> str:
    """SSE 帧。`data` 必须是 JSON —— 前端手工解析 `data:` 行。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 图节点 → 面向用户的阶段（§3.7.2 的取值表）。
# ⚠️ 阶段**与内部节点名解耦**：节点重构不该改 SSE 协议 —— 所以映射只此一份。
# ⚠️ `clarify` 与 `refuse` **刻意不在此表里**：
#    - clarify 之后用户看到的是一句反问、不是答案，发 `generating` 是骗人；
#    - refuse 是 rerank 之后零成本短路，没有可提示的耗时。
#    （回归锁：`tests/integration/test_stage_events.py`）
NODE_TO_STAGE = {
    "resolve": "resolving",
    "route": "routing",
    "rewrite": "retrieving",
    "retrieve": "retrieving",
    "rerank": "reranking",
    "build_context": "generating",
    "generate": "generating",
    "cite": "verifying",
    "chat": "generating",
}

# ⚠️ 展示文案**以 `stage` 为准**（§4.2.4.2 的文案映射表在**前端**）：
#    label 只是兜底与调试。两个文案源并存必然不一致 ——
#    服务端改了 label，前端还显示自己那份。
STAGE_LABELS = {
    "resolving": "正在理解你的问题…",
    "routing": "正在判断问题类型…",
    "retrieving": "正在检索知识库…",
    "reranking": "正在筛选最相关的资料…",
    "generating": "正在组织答案…",
    "verifying": "正在核对引用…",
}


def stage_of(payload: dict) -> str | None:
    """`debug` 通道的「节点开工」事件 → stage 名；不是开工事件就 None。

    ⚠️ 必须**宽容**：图会继续长（M5 还要加评测链路），映射表里没有的节点、
       形状变化的载荷都只能安静跳过 —— 一个未知节点名不该让整条 SSE 流中断。

    ⚠️ 只认 `type == "task"`：同一通道还会回放 `task_result`（节点**结束**），
       拿它当阶段提示就晚了一整步。
    """
    if payload.get("type") != "task":
        return None
    inner = payload.get("payload")
    name = inner.get("name") if isinstance(inner, dict) else None
    return NODE_TO_STAGE.get(name or "")


def node_ran(state: dict, node: str) -> bool:
    """该节点是否真的执行过 —— 看 trace 累积（有 operator.add reducer，不会丢）。

    ⚠️ 不能靠某个字段"有值"来判断：`new_state()` 给多数字段设了默认值，
       第一个快照就全都有值，会把还没跑的节点误判成已跑。
       也不能假设每个节点都写了 trace —— 有 4 个节点曾漏写，
       导致此处恒为 False、事件静默缺失（已加回归锁 `test_graph_trace.py`）。
    """
    return any(entry.get("node") == node for entry in (state.get("trace") or []))


# 澄清到顶时的说明。**文案的唯一来源是服务端**（§3.7.2 同一条口径）——
# 前端不得按 reason 自造，否则两边话术迟早不一致。
CLARIFY_LIMIT_NOTE = (
    "我理解你想问的是【{query}】。以下回答基于这个理解。"
    "如果你实际想问的是别的，请重新描述。"
)


async def _clarify_counts(session_id: str) -> tuple[int, int]:
    """返回 (紧邻本轮的连续澄清轮次, 本会话累计澄清次数)。

    ⚠️ 「链」必须是**紧邻**的：中间只要有一轮不是澄清就断了 ——
       否则用户换了话题还会被上一轮的账压着。
    ⚠️ 从 `messages` 数：`route` 落在**用户消息**那一行（助手行没有 route）。
    """
    async with db.tx() as conn:
        rows = await conn.fetch(
            """SELECT route FROM messages
                WHERE conversation_id = $1 AND role = 'user'
                ORDER BY created_at ASC""",
            session_id,
        )
    routes = [r["route"] for r in rows]
    total = sum(1 for r in routes if r == "clarify")
    chain = 0
    for r in reversed(routes):
        if r != "clarify":
            break
        chain += 1
    return chain, total


def holder_id() -> str:
    """长锁持有者标识（实例 id + pid），排查「谁卡着」用。"""
    return f"{uuid.uuid4().hex[:8]}:{os.getpid()}"


async def stream_chat(
    *,
    query: str,
    session_id: str,
    is_new_session: bool,
    include_restricted: bool,
    user: UserContext,
    is_disconnected: DisconnectCheck,
) -> AsyncIterator[str]:
    """问答主流程，逐帧产出 SSE 文本。

    调用方（api 层）负责把它包成 StreamingResponse。
    """
    holder = holder_id()
    started = time.perf_counter()

    # ---- 会话长锁：抢不到就拒绝，不排队（§3.2.4）--------------------
    if not await db.acquire_long_lock(session_id, holder):
        yield sse("error", {"code": "session_busy",
                            "message": "本会话正在生成中，请等待完成或新开会话"})
        return

    heartbeat_task: asyncio.Task | None = None
    try:
        if is_new_session:
            await create_conversation(session_id, user.id, title=query[:20])
            yield sse("session_created", {"session_id": session_id})

        heartbeat_task = asyncio.create_task(_heartbeat(session_id, holder))

        # 历史 = 摘要 + messages[compressed_count:]；超水位会在这里**同步压缩**
        #（§3.8.3）。压缩失败只记降级，本轮照常作答 —— 绝不变用户 500。
        slice_ = await context_service.load_context(session_id)
        clarify_chain, clarify_total = await _clarify_counts(session_id)
        state = new_state(
            query=query,
            session_id=session_id,
            user=UserContextLite(id=user.id, role=user.role),
            history=slice_.messages,
            summary=slice_.summary,
            last_route=await _last_route(session_id),
            clarify_chain=clarify_chain,
            clarify_total=clarify_total,
            # 非 admin 传了按 false 处理 —— filters.resolve_escalation 兜底
            include_restricted=bool(include_restricted) and user.role == "admin",
            # 压缩的耗时与降级要进 node_timings（静默兜底 = 评测护栏看不见）
            trace=[slice_.trace()],
        )

        final: dict = {}
        emitted_route = False
        emitted_token = False
        # 节点自己发的 error（generate 的降级、chat/clarify 的上游故障）——
        # `error` 是**终止事件**，见下面的收尾段
        emitted_error = False
        last_stage: str | None = None

        # `debug` 通道给出**节点开工**信号 —— 阶段提示靠它，
        # 而不是靠 `values` 快照：快照是「节点跑完之后」才有的，
        # 那一刻再报「正在检索知识库…」已经晚了一步（静默期早过了）。
        async for mode, payload in get_graph().astream(
            state, stream_mode=["custom", "values", "debug"]
        ):
            if await is_disconnected():
                # ⚠️ 客户端断连也必须释放锁 —— 否则该会话会永久「正在生成」，
                #    前端按钮禁用 + 后端拒绝，学生只能重开会话
                logger.info("客户端断连，终止生成",
                            extra={"event": "chat.disconnected", "session_id": session_id})
                break

            if mode == "values":
                final = payload or {}

                # ⚠️ 必须确认 route 节点**真的跑过**再发 route 事件：
                #    new_state() 给 route 设了默认值 "knowledge"，
                #    若只看 `final.get("route")`，resolve 之后的第一个快照
                #    就会带着这个默认值把 route 事件发出去 ——
                #    实际路由到 clarify 的查询会被前端显示成 knowledge。
                # ⚠️ clarify 的 route 事件要等 clarify 节点跑完再发：
                #    `clarify_facets` 是 clarify 节点产出的，route 节点刚跑完时
                #    它还是空数组 —— 那一刻发出去，前端永远拿不到可点选项。
                clarify_ready = (final.get("route") != "clarify"
                                 or node_ran(final, "clarify"))
                if not emitted_route and node_ran(final, "route") and clarify_ready:
                    emitted_route = True
                    # 澄清到顶被跳过时，先把说明发出去 —— 文案由服务端产出
                    if final.get("clarify_skipped"):
                        yield sse("clarify_skipped",
                                  {"text": CLARIFY_LIMIT_NOTE.format(query=query)})
                    event: dict = {"route": final["route"]}
                    if final.get("route") == "clarify":
                        # ⚠️ 字段名是 clarify_facets，不是 facets
                        event["clarify_facets"] = final.get("clarify_facets") or []
                    yield sse("route", event)
                    if final.get("resolved_query") and final["resolved_query"] != query:
                        yield sse("resolved", {"resolved_query": final["resolved_query"]})
            elif mode == "debug":
                # 阶段提示：填充首个 token 到达前的静默期（§4.2.4.2）。
                # ⚠️ 连续同名只发一次 —— build_context 与 generate 都归
                #    `generating`，连发两次只会让提示闪一下再回到同一句。
                # ⚠️ error 之后一律停：`error` 是终止事件（§3.7.2），
                #    这之后再冒「正在核对引用…」等于告诉用户还在正常往下走。
                stage = stage_of(payload)
                if stage and not emitted_error and stage != last_stage:
                    last_stage = stage
                    yield sse("stage", {"stage": stage,
                                        "label": STAGE_LABELS.get(stage, "")})
            elif mode == "custom":
                # generate / chat / clarify 节点边收边发的 token / error
                if payload.get("type") == "token":
                    emitted_token = True
                    yield sse("token", {"text": payload["text"]})
                elif payload.get("type") == "error":
                    emitted_error = True
                    yield sse("error", {"code": payload.get("code", "upstream_error"),
                                        "message": payload.get("message", "")})

        # ---- 收尾事件 ------------------------------------------------
        decision = final.get("decision") or ""
        latency_ms = int((time.perf_counter() - started) * 1000)
        generate_ran = node_ran(final, "generate")

        # ⚠️ `error` 是**终止事件**（§3.7.2）：发完 error 之后不再发
        #    decision / citations / verify / refused / done。
        #    尤其**不发 `refused`** —— 上游格式错误不是拒答（§3.5.3 节点 9
        #    降级规则第 ③ 条），发了就把「模型输出坏了」记成「知识库没依据」。
        #    落库照旧：坏掉的这一轮也要能在 qa_logs 里排查。
        if emitted_error:
            await _persist(session_id, query, final, latency_ms, user, decision,
                           route=final.get("route", ""))
            return

        # ⚠️ 保底：chat / clarify 走非流式兜底路径时不会产生 token 事件，
        #    用户会看到**完全空白**的回复（不报错、也没有内容）——
        #    这比报错更难发现。任何路径只要有答案却一个 token 都没发过，就在这里补发。
        # ⚠️ 拒答路径**不要**在这里补发：`answer` 就是服务端拒答文案，
        #    补发会让它在气泡里渲染一遍、`refused` 框里再渲染一遍（M3 评审抓出）。
        if not emitted_token and not final.get("refused") and (final.get("answer") or "").strip():
            yield sse("token", {"text": final["answer"]})
            emitted_token = True

        if final.get("refused"):
            # 两条拒答路径都发 refused；decision 只在 generate 跑过后才有意义
            reason = final.get("refusal_reason") or "no_candidate"
            if decision == "REFUSED_NO_EVIDENCE":
                yield sse("decision", {"decision": "REFUSED_NO_EVIDENCE"})
            payload = {"reason": reason, "text": final.get("answer", "")}
            if reason == "no_candidate":
                payload["hint"] = "该类问题建议咨询教务处或相关职能部门。"
            yield sse("refused", payload)
        elif final.get("route") == "knowledge" and generate_ran:
            # ⚠️ 只有 knowledge 这条路径才发 citations / verify / decision
            #    （chat / clarify 两条不发）
            yield sse("decision", {"decision": decision or "ANSWERED"})
            citations = [c.model_dump() if hasattr(c, "model_dump") else c
                         for c in (final.get("citations") or [])]
            yield sse("citations", {"citations": citations})
            report = final.get("verify_report")
            if report is not None:
                yield sse("verify", report.model_dump()
                          if hasattr(report, "model_dump") else report)

        # ---- 落库 ----------------------------------------------------
        await _persist(session_id, query, final, latency_ms, user, decision,
                       route=final.get("route", ""))

        yield sse("done", {"latency_ms": latency_ms})

    except Exception:  # noqa: BLE001 —— 任何异常都转成 error 事件，不发 done
        logger.exception("问答失败", extra={"event": "chat.failed",
                                          "session_id": session_id})
        yield sse("error", {"code": "internal", "message": "服务器内部错误"})
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
        # ⚠️ 释放必须覆盖三条路径：正常结束 / 超时 / 客户端断连
        await db.release_long_lock(session_id, holder)


async def _heartbeat(session_id: str, holder: str) -> None:
    """生成过程中每 30 秒续期一次（§3.2.4）。"""
    interval = int(cfg("session.lock_heartbeat_seconds", 30))
    try:
        while True:
            await asyncio.sleep(interval)
            if not await db.heartbeat_long_lock(session_id, holder):
                # 锁已被抢走 —— 停止生成，别再写脏数据
                logger.warning("会话锁已易主，心跳停止",
                               extra={"event": "chat.lock_lost", "session_id": session_id})
                return
    except asyncio.CancelledError:
        return


async def _last_route(session_id: str) -> str:
    """上一条用户消息被路由到哪一类 —— `last_route` 的来源（§3.3.1）。"""
    async with db.tx() as conn:
        row = await conn.fetchrow(
            """SELECT route FROM messages
                WHERE conversation_id = $1 AND role = 'user' AND route IS NOT NULL
                ORDER BY created_at DESC LIMIT 1""",
            session_id,
        )
    return row["route"] if row else ""


async def _persist(session_id: str, query: str, final: dict, latency_ms: int,
                   user: UserContext, decision: str, *, route: str) -> None:
    """落库。⚠️ 写在短锁保护下，且**与写操作同一个事务**（§3.3.5）。"""
    answer = final.get("answer") or ""
    citations = [c.model_dump() if hasattr(c, "model_dump") else c
                 for c in (final.get("citations") or [])]

    try:
        async with db.tx() as conn:
            # 短锁必须与写操作在同一事务 —— 否则锁在写之前就释放了
            await db.short_lock(conn, session_id)
            await append_messages(conn, session_id, query, answer, citations, route)
            await touch_conversation(conn, session_id)
    except Exception:  # noqa: BLE001 —— 落库失败不该让用户看不到回答
        logger.exception("落库失败", extra={"event": "chat.persist_failed",
                                          "session_id": session_id})

    try:
        await write_qa_log(
            session_id=session_id,
            user_id=user.id,
            user_role=user.role,
            trace_id=current_trace_id(),
            query=query,
            resolved_query=final.get("resolved_query", ""),
            route=route,
            retrieved=[c.chunk_id for c in (final.get("candidates") or [])],
            reranked=[c.chunk_id for c in (final.get("reranked") or [])],
            answer=answer,
            refused=bool(final.get("refused")),
            refusal_reason=final.get("refusal_reason") or None,
            verify_report=(final.get("verify_report").model_dump()
                           if hasattr(final.get("verify_report"), "model_dump") else None),
            route_source=final.get("route_source", ""),
            # 澄清到顶被跳过的那一轮 —— 否则与管理员的普通提问分不出来
            clarify_skipped=bool(final.get("clarify_skipped")),
            degraded=bool(final.get("rerank_degraded")),
            latency_ms=latency_ms,
            node_timings=final.get("trace") or [],
        )
    except Exception:  # noqa: BLE001
        logger.exception("写 qa_logs 失败", extra={"event": "chat.qalog_failed"})
