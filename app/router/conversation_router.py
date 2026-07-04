"""会话记忆 REST API 路由。"""
from fastapi import APIRouter, Query
from app.config.loader import get_config
from app.router.conversation_service import ConversationService
from app.core.success_response import success_response
from app.core.failed_response import AppException

conversation_router = APIRouter(prefix="/conversation", tags=["conversation"])
_svc = ConversationService()


@conversation_router.post("/new")
async def create_conversation(user_id: str = Query(...), title: str = Query("")):
    result = _svc.create(user_id, title)
    return success_response(result, "会话创建成功")


@conversation_router.get("/list")
async def list_conversations(
    user_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(get_config("pagination_default_limit", 20), ge=1, le=100),
):
    """分页获取用户会话列表（过滤已删除，置顶优先 + 最近聊天靠前）。"""
    conversations = _svc.list_user_conversations(user_id, offset, limit)
    return success_response({"conversations": conversations, "offset": offset, "limit": limit})


@conversation_router.get("/{session_id}/messages")
async def get_messages(session_id: str):
    messages = _svc.get_messages(session_id)
    return success_response({"session_id": session_id, "messages": messages})


@conversation_router.post("/{session_id}/pin")
async def toggle_pin(session_id: str, is_top: bool = Query(...)):
    """切换会话置顶状态。"""
    _svc.toggle_pin(session_id, is_top)
    return success_response({"session_id": session_id, "is_top": is_top}, "置顶状态已更新")


@conversation_router.delete("/{session_id}")
async def delete_conversation(session_id: str):
    """删除会话及所有关联消息（硬删除）。"""
    _svc.delete(session_id)
    return success_response(None, "会话已删除")


@conversation_router.delete("/clear/{user_id}")
async def clear_user_conversations(user_id: str):
    """删除用户所有会话及关联消息（硬删除）。"""
    _svc.clear_user(user_id)
    return success_response(None, "用户会话已清空")
