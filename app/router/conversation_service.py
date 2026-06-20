"""会话管理业务逻辑层。"""
from app.memory.memory_service import ConversationMemoryService
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ConversationService:
    """会话管理业务逻辑。"""

    def __init__(self):
        self._memory = ConversationMemoryService()

    def create(self, user_id: str, title: str = "") -> dict:
        session_id = self._memory.create_conversation(user_id, title)
        return {"session_id": session_id, "user_id": user_id, "title": title}

    def list_user_conversations(self, user_id: str) -> list[dict]:
        return self._memory.get_user_conversations(user_id)

    def get_messages(self, session_id: str) -> list[dict]:
        return self._memory.get_conversation_messages(session_id)

    def delete(self, session_id: str):
        self._memory.delete_conversation(session_id)

    def clear_user(self, user_id: str):
        self._memory.clear_user(user_id)

    def load_context(self, session_id: str, max_turns: int = 10) -> list:
        return self._memory.load_context(session_id, max_turns)

    def append(self, session_id: str, human_msg: str, ai_msg: str):
        self._memory.append_messages(session_id, human_msg, ai_msg)
