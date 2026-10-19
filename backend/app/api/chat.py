"""User 端问答路由（§3.7.2）。

**分层**（方案 §3.1）：本文件只做参数校验与响应封装，
业务（会话锁、图执行、SSE 组装、落库）全在 `services/chat_service.py`。

SSE 的协议细节与铁律见 `services/chat_service.py` 的模块说明。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core import observability, trace
from app.core.deps import CurrentUser
from app.schemas.chat import ChatRequest
from app.services import chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/stream")
async def chat_stream(body: ChatRequest, user: CurrentUser, request: Request):
    session_id = body.session_id or uuid.uuid4().hex
    is_new_session = body.session_id is None

    # ⚠️ Starlette 的 StreamingResponse 生成器可能跑在另一个 context 里，
    #    trace 会丢 —— 先取出来，进生成器第一件事就是挂回来（§3.2.3.1）
    trace_id = observability.current_trace_id()
    if not trace_id or not trace_id.strip("0"):
        # 直连测试/无中间件调用时也要有合法 trace，否则日志全是 0
        trace_id = trace.new_trace_id()
    span_id = observability.current_span_id()

    # 排查入口：控制台直接给出本轮 trace_id，与响应头 x-trace-id 同源。
    # 消息里带一遍是为了在纯文本日志里也能一眼看到。
    logger.info("对话开始 trace_id=%s", trace_id,
                extra={"event": "chat.start", "session_id": session_id,
                       "trace_id": trace_id})

    async def generator():
        with trace.begin_trace(trace_id), observability.attach_trace(trace_id, span_id):
            async for frame in chat_service.stream_chat(
                query=body.query,
                session_id=session_id,
                is_new_session=is_new_session,
                include_restricted=bool(body.include_restricted),
                user=user,
                is_disconnected=request.is_disconnected,
            ):
                yield frame

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers=chat_service.SSE_HEADERS)
