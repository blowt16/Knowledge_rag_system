"""会话接口（§3.7.2）。

**分层**（A7）：这里只做参数校验与响应封装，业务在
`app/services/conversation_service.py`。

⚠️ 越权一律 **404**（M4-D4）—— 由 service 抛 `NotFound`，不在这里判。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser
from app.core.exceptions import AppError
from app.schemas.conversation import (
    ConversationItem,
    ConversationListResponse,
    CreateConversationRequest,
    MessageItem,
    MessagesResponse,
    PatchConversationRequest,
)
from app.services import conversation_service as convs

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> ConversationListResponse:
    """排序固定 `is_top DESC, last_chat_time DESC`；软删的不出现也不计数。"""
    return ConversationListResponse(**await convs.list_conversations(
        user.id, offset=offset, limit=limit))


@router.post("", response_model=ConversationItem)
async def create_conversation(
    body: CreateConversationRequest, user: CurrentUser
) -> ConversationItem:
    """先建空会话。

    ⚠️ 前端**正常提问不走这条** —— 点「新建对话」只是清空本地 `session_id`，
       会话由问答流按需创建；否则列表里会堆一屏没有消息的空会话（§3.7.2）。
    """
    return ConversationItem(**await convs.create_for_user(
        user.id, title=body.title or ""))


@router.get("/{session_id}/messages", response_model=MessagesResponse)
async def get_messages(session_id: str, user: CurrentUser) -> MessagesResponse:
    return MessagesResponse(messages=[
        MessageItem(**m) for m in await convs.get_messages(session_id, user.id)
    ])


@router.patch("/{session_id}", response_model=ConversationItem)
async def patch_conversation(
    session_id: str, body: PatchConversationRequest, user: CurrentUser
) -> ConversationItem:
    if body.title is None and body.is_top is None:
        # 两个字段至少给一个（§3.7.2）—— 空 PATCH 是无意义请求，别静默放过
        raise AppError("至少要给出 title 或 is_top 之一")
    return ConversationItem(**await convs.patch_conversation(
        session_id, user.id, title=body.title, is_top=body.is_top))


@router.delete("/{session_id}")
async def delete_conversation(session_id: str, user: CurrentUser) -> dict:
    await convs.delete_conversation(session_id, user.id)
    return {"message": "已删除"}
