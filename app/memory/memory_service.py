"""会话记忆服务 — LangChain SQLChatMessageHistory + SQLite 持久化。"""
import os
import sqlite3
import uuid
from datetime import datetime
from app.utils.path_tool import get_db_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ConversationMemoryService:
    """会话记忆服务：管理多轮对话的存储与加载。

    双层表结构：
    - conversations: 自定义会话元信息表
    - message_store: LangChain SQLChatMessageHistory 自动管理
    """

    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or get_db_path("conversation.db"))
        self._ensure_tables()

    def _ensure_tables(self):
        """确保 SQLite 表和索引存在。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id         TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    title      TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_store (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message    TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_store_session_id
                ON message_store(session_id)
            """)
            conn.commit()
            logger.info("SQLite 数据库初始化完成")
        finally:
            conn.close()

    def get_message_history(self, session_id: str):
        """获取指定会话的 LangChain 消息历史对象。"""
        from langchain_community.chat_message_histories import SQLChatMessageHistory
        return SQLChatMessageHistory(
            session_id=session_id,
            connection=f"sqlite:///{self._db_path}",
        )

    def get_memory(self, session_id: str):
        """创建带记忆的对话缓冲区。"""
        from langchain.memory import ConversationBufferMemory

        history = self.get_message_history(session_id)
        return ConversationBufferMemory(
            chat_memory=history,
            return_messages=True,
            memory_key="chat_history",
        )

    def load_context(self, session_id: str, max_turns: int = None) -> list:
        """加载最近 N 轮对话作为上下文。

        Args:
            session_id: 会话 ID
            max_turns: 最大加载轮数（默认从环境变量 MAX_MEMORY_TURNS 读取）

        Returns:
            LangChain Message 对象列表
        """
        if max_turns is None:
            max_turns = int(os.getenv("MAX_MEMORY_TURNS", "10"))

        history = self.get_message_history(session_id)
        messages = history.messages
        return messages[-(max_turns * 2):]  # 每轮含 human + ai 两条

    def append_messages(self, session_id: str, human_msg: str, ai_msg: str):
        """追加一轮对话。"""
        history = self.get_message_history(session_id)
        from langchain_core.messages import HumanMessage, AIMessage
        history.add_messages([
            HumanMessage(content=human_msg),
            AIMessage(content=ai_msg),
        ])
        self._update_conversation_timestamp(session_id)

    def create_conversation(self, user_id: str, title: str = "") -> str:
        """创建新会话，返回 session_id。"""
        session_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, title or "", now, now),
            )
            conn.commit()
            logger.info(f"【会话记忆】创建会话: {session_id}")
        finally:
            conn.close()

        return session_id

    def get_user_conversations(self, user_id: str) -> list[dict]:
        """获取用户的所有会话列表（按更新时间倒序）。"""
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            )
            rows = cursor.fetchall()
            return [
                {"id": r[0], "user_id": r[1], "title": r[2], "created_at": r[3], "updated_at": r[4]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_conversation_messages(self, session_id: str) -> list[dict]:
        """获取会话的全部消息。"""
        history = self.get_message_history(session_id)
        messages = history.messages
        result = []
        for msg in messages:
            role = getattr(msg, "type", "unknown")
            content = getattr(msg, "content", str(msg))
            result.append({"role": role, "content": content})
        return result

    def delete_conversation(self, session_id: str):
        """删除会话及所有关联消息。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM conversations WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (session_id,))
            conn.commit()
            logger.info(f"【会话记忆】删除会话: {session_id}")
        finally:
            conn.close()

    def clear_user(self, user_id: str):
        """清空用户所有会话。"""
        conn = sqlite3.connect(self._db_path)
        try:
            sessions = conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchall()
            for (sid,) in sessions:
                conn.execute("DELETE FROM message_store WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            conn.commit()
            logger.info(f"【会话记忆】清空用户 {user_id} 全部会话")
        finally:
            conn.close()

    def _update_conversation_timestamp(self, session_id: str):
        """更新会话的 updated_at 时间戳。"""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
        finally:
            conn.close()
