"""会话管理业务逻辑层。"""
from app.config.loader import get_config
from app.memory.memory_service import ConversationMemoryService
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ConversationService:
    """会话管理业务逻辑。"""

    def __init__(self):
        self._memory = ConversationMemoryService.get_shared()

    def create(self, user_id: str, title: str = "") -> dict:
        session_id = self._memory.create_conversation(user_id, title)
        return {"session_id": session_id, "user_id": user_id, "title": title}

    def list_user_conversations(self, user_id: str, offset: int = 0, limit: int | None = None) -> list[dict]:
        if limit is None:
            limit = int(get_config("pagination_default_limit", 20))
        return self._memory.get_user_conversations(user_id, offset, limit)

    def get_messages(self, session_id: str) -> list[dict]:
        return self._memory.get_conversation_messages(session_id)

    def toggle_pin(self, session_id: str, is_top: bool):
        self._memory.toggle_pin(session_id, is_top)

    def delete(self, session_id: str):
        self._memory.delete_conversation(session_id)

    def clear_user(self, user_id: str):
        self._memory.clear_user(user_id)

    def load_context(self, session_id: str, max_turns: int = None) -> list:
        return self._memory.load_context(session_id, max_turns)

    def append(self, session_id: str, human_msg: str, ai_msg: str):
        self._memory.append_messages(session_id, human_msg, ai_msg)
