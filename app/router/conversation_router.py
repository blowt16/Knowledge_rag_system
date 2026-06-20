"""会话记忆 REST API 路由。"""
from fastapi import APIRouter, Query
from app.router.conversation_service import ConversationService
from app.core.success_response import success_response

conversation_router = APIRouter(prefix="/conversation", tags=["conversation"])
_svc = ConversationService()


@conversation_router.post("/new")
async def create_conversation(user_id: str = Query(...), title: str = Query("")):
    """创建新会话。"""
    result = _svc.create(user_id, title)
    return success_response(result, "会话创建成功")


@conversation_router.get("/list")
async def list_conversations(user_id: str = Query(...)):
    """获取用户会话列表。"""
    conversations = _svc.list_user_conversations(user_id)
    return success_response({"conversations": conversations, "total": len(conversations)})


@conversation_router.get("/{session_id}/messages")
async def get_messages(session_id: str):
    """获取会话的全部消息。"""
    messages = _svc.get_messages(session_id)
    return success_response({"session_id": session_id, "messages": messages})


@conversation_router.delete("/{session_id}")
async def delete_conversation(session_id: str):
    """删除会话及所有关联消息。"""
    _svc.delete(session_id)
    return success_response(None, "会话已删除")


@conversation_router.delete("/clear/{user_id}")
async def clear_user_conversations(user_id: str):
    """清空用户所有会话。"""
    _svc.clear_user(user_id)
    return success_response(None, "用户会话已清空")
