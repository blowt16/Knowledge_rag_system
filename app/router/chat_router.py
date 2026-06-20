"""统一对话入口 — Agent + RAG + 会话管理。"""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from app.router.chat_service import ChatService
from app.schemas.models import ChatRequest

chat_router = APIRouter(prefix="/chat", tags=["chat"])


@chat_router.post("")
async def chat(request: ChatRequest):
    """统一对话入口：Agent 编排 + RAG 检索 + 会话管理 + SSE 流式输出。"""
    service = ChatService()
    return StreamingResponse(
        service.handle_chat(
            query=request.query,
            session_id=request.session_id,
            user_id=request.user_id,
        ),
        media_type="text/event-stream",
    )
