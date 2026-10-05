"""User 端问答路由（§3.7.2）。

**分层**（方案 §3.1）：本文件只做参数校验与响应封装，
业务（会话锁、图执行、SSE 组装、落库）全在 `services/chat_service.py`。

SSE 的协议细节与铁律见 `services/chat_service.py` 的模块说明。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUser
from app.core.telemetry import current_span_id, current_trace_id, reattach
from app.schemas.chat import ChatRequest
from app.services import chat_service

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/stream")
async def chat_stream(body: ChatRequest, user: CurrentUser, request: Request):
    session_id = body.session_id or uuid.uuid4().hex
    is_new_session = body.session_id is None

    # ⚠️ Starlette 的 StreamingResponse 生成器可能跑在另一个 context 里，
    #    trace 会丢 —— 先取出来，进生成器第一件事就是挂回来（§3.2.3.1）
    trace_id, span_id = current_trace_id(), current_span_id()

    async def generator():
        with reattach(trace_id, span_id):
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
