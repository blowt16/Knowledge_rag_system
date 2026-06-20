"""对话业务逻辑层 — 统一对话入口。"""
import json
import uuid
from typing import AsyncIterator
from app.memory.memory_service import ConversationMemoryService
from app.rag.agent.agent_service import AgentService
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ChatService:
    """统一对话服务：Agent + 会话管理。"""

    def __init__(self):
        self._memory = ConversationMemoryService()
        self._agent_svc = AgentService()

    async def handle_chat(self, query: str, session_id: str | None,
                          user_id: str) -> AsyncIterator[str]:
        """处理对话请求，SSE 流式输出。

        Yields:
            SSE 格式字符串: "data: {json}\n\n"
        """
        # 1. 会话管理
        if not session_id:
            session_id = self._memory.create_conversation(user_id, query[:30])
            yield f"data: {json.dumps({'event': 'session_created', 'session_id': session_id})}\n\n"

        # 2. 流式执行 Agent
        try:
            async for event in self._agent_svc.stream_chat(
                query=query, session_id=session_id, user_id=user_id
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"对话处理失败: {e}")
            yield f"data: {json.dumps({'event': 'error', 'data': str(e)})}\n\n"

        # 3. 持久化由 RunnableWithMessageHistory 自动完成
