"""会话记忆服务 — LangChain SQLChatMessageHistory + SQLite 持久化。"""
import json
import sqlite3
import uuid
from datetime import datetime
from app.config.loader import get_config
from app.utils.path_tool import get_db_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _title_max_len() -> int:
    return int(get_config("session_title_max_length", 20))


def _truncate_title(text: str) -> str:
    """截断标题到固定长度。"""
    ml = _title_max_len()
    if len(text) <= ml:
        return text
    return text[:ml] + "..."


class ConversationMemoryService:
    """会话记忆服务：管理多轮对话的存储与加载。

    表结构：
    - conversations: 会话元信息（is_top / last_chat_time / session_title）
    - message_store: LangChain SQLChatMessageHistory 自动管理
    """

    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or get_db_path("conversation.db"))
        self._ensure_tables()
        self._migrate()
        self._ensure_indexes()

    def _ensure_tables(self):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id             TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    session_title  TEXT DEFAULT '',
                    is_top         INTEGER DEFAULT 0,
                    last_chat_time TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
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

    def _ensure_indexes(self):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user_time
                ON conversations(user_id, is_top, last_chat_time)
            """)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self):
        """增量迁移：兼容旧表结构。"""
        conn = sqlite3.connect(self._db_path)
        try:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
            migrations = [
                ("session_title", "ALTER TABLE conversations ADD COLUMN session_title TEXT DEFAULT ''"),
                ("is_top", "ALTER TABLE conversations ADD COLUMN is_top INTEGER DEFAULT 0"),
                ("delete_flag", "ALTER TABLE conversations ADD COLUMN delete_flag INTEGER DEFAULT 0"),
                ("last_chat_time", "ALTER TABLE conversations ADD COLUMN last_chat_time TEXT NOT NULL DEFAULT ''"),
            ]
            for col, sql in migrations:
                if col not in existing:
                    conn.execute(sql)
                    logger.info(f"数据库迁移: 添加列 {col}")

            # 补齐旧数据的 last_chat_time
            conn.execute(
                "UPDATE conversations SET last_chat_time = created_at WHERE last_chat_time = '' OR last_chat_time IS NULL"
            )

            # 补齐旧数据的 session_title：用 Python 解析 JSON 提取 human 消息 content
            rows = conn.execute(
                "SELECT id FROM conversations WHERE (session_title = '' OR session_title IS NULL)"
            ).fetchall()
            for (cid,) in rows:
                msg_row = conn.execute(
                    "SELECT message FROM message_store WHERE session_id = ? LIMIT 1", (cid,)
                ).fetchone()
                if msg_row:
                    try:
                        data = json.loads(msg_row[0])
                        content = data.get("content", "")
                        if content:
                            conn.execute(
                                "UPDATE conversations SET session_title = ? WHERE id = ?",
                                (_truncate_title(content), cid),
                            )
                    except (json.JSONDecodeError, KeyError):
                        pass

            conn.commit()
        finally:
            conn.close()

    # ============================================================
    # 消息历史（原始 SQL，去除 SQLChatMessageHistory 依赖）
    # ============================================================

    def load_context(self, session_id: str, max_turns: int = None) -> list:
        if max_turns is None:
            from app.config.loader import get_config
            max_turns = int(get_config("llm_history_turns", 5))
        rows = self._read_messages(session_id)
        messages = []
        from langchain_core.messages import HumanMessage, AIMessage
        for msg_type, content in rows[-(max_turns * 2):]:
            if msg_type == "human":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))
        return messages

    def append_messages(self, session_id: str, human_msg: str, ai_msg: str) -> bool:
        conn = sqlite3.connect(self._db_path)
        try:
            h = json.dumps({"type": "human", "content": human_msg}, ensure_ascii=False)
            a = json.dumps({"type": "ai", "content": ai_msg}, ensure_ascii=False)
            conn.execute(
                "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                (session_id, h),
            )
            conn.execute(
                "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                (session_id, a),
            )

            # 更新 last_chat_time，若标题为空则用第一条 human 消息补写
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "UPDATE conversations SET last_chat_time = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            conn.execute(
                "UPDATE conversations SET session_title = ? WHERE id = ? AND (session_title = '' OR session_title IS NULL)",
                (_truncate_title(human_msg), session_id),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"【会话记忆】消息持久化失败: {e}")
            return False
        finally:
            conn.close()

    def _read_messages(self, session_id: str) -> list:
        """读取消息原始数据，返回 [(type, content), ...]"""
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT message FROM message_store WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            result = []
            for (msg,) in rows:
                try:
                    data = json.loads(msg)
                    result.append((data.get("type", "unknown"), data.get("content", "")))
                except json.JSONDecodeError:
                    result.append(("unknown", msg))
            return result
        finally:
            conn.close()

    # ============================================================
    # 会话 CRUD
    # ============================================================

    def create_conversation(self, user_id: str, title: str = "") -> str:
        session_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO conversations
                   (id, user_id, session_title, is_top, last_chat_time, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (session_id, user_id, _truncate_title(title) if title else "", now, now, now),
            )
            conn.commit()
            logger.debug(f"【会话记忆】创建会话: {session_id}")
        finally:
            conn.close()
        return session_id

    def get_user_conversations(self, user_id: str, offset: int = 0, limit: int | None = None) -> list[dict]:
        if limit is None:
            limit = int(get_config("pagination_default_limit", 20))
        """分页获取用户会话列表（过滤已删除，置顶优先 + 最近聊天靠前）。"""
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                """SELECT id, session_title, is_top, last_chat_time
                   FROM conversations
                   WHERE user_id = ?
                   ORDER BY is_top DESC, last_chat_time DESC
                   LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            )
            rows = cursor.fetchall()
            return [
                {"session_id": r[0], "session_title": r[1], "is_top": r[2], "last_chat_time": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_conversation_messages(self, session_id: str) -> list[dict]:
        rows = self._read_messages(session_id)
        result = []
        for msg_type, content in rows:
            result.append({"role": msg_type, "content": content})
        return result

    def toggle_pin(self, session_id: str, is_top: bool):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE conversations SET is_top = ? WHERE id = ?",
                (1 if is_top else 0, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def touch_conversation(self, session_id: str, first_query: str = ""):
        """更新会话时间戳，若标题为空则用首条 query 补写（Agent 模式用）。"""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE conversations SET last_chat_time = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            if first_query:
                conn.execute(
                    "UPDATE conversations SET session_title = ? WHERE id = ? AND (session_title = '' OR session_title IS NULL)",
                    (_truncate_title(first_query), session_id),
                )
            conn.commit()
        finally:
            conn.close()

    def update_title(self, session_id: str, title: str):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE conversations SET session_title = ? WHERE id = ?",
                (_truncate_title(title), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_conversation(self, session_id: str):
        """硬删除：移除会话记录及全部关联消息。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (session_id,))
            conn.execute("COMMIT")
            logger.debug(f"【会话记忆】硬删除会话: {session_id}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def clear_user(self, user_id: str):
        """硬删除用户所有会话及关联消息。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN TRANSACTION")
            sessions = conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchall()
            for (sid,) in sessions:
                conn.execute("DELETE FROM message_store WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            conn.execute("COMMIT")
            logger.info(f"【会话记忆】硬删除用户 {user_id} 全部会话")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
