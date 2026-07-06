"""会话记忆服务 — SQLite 持久化，使用持久连接消除重复 connect/close 开销。"""
import json
import sqlite3
import uuid
from datetime import datetime
from app.config.loader import get_config
from app.utils.path_tool import get_db_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)
_shared_instance = None


def _title_max_len() -> int:
    return int(get_config("session_title_max_length", 20))


def _truncate_title(text: str) -> str:
    ml = _title_max_len()
    if len(text) <= ml:
        return text
    return text[:ml] + "..."


class ConversationMemoryService:
    """会话记忆服务：管理多轮对话的存储与加载。

    使用持久 SQLite 连接 + WAL 模式，避免每次操作 connect/close 的 I/O 开销。
    通过 get_shared() 获取全局单例，Agent 模式不再重复初始化。
    """

    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or get_db_path("conversation.db"))
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()
        self._migrate()
        self._ensure_indexes()

    @classmethod
    def get_shared(cls) -> "ConversationMemoryService":
        global _shared_instance
        if _shared_instance is None:
            _shared_instance = cls()
        return _shared_instance

    def _ensure_tables(self):
        conn = self._conn
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

    def _ensure_indexes(self):
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_user_time
            ON conversations(user_id, is_top, last_chat_time)
        """)
        self._conn.commit()

    def _migrate(self):
        conn = self._conn
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

        conn.execute(
            "UPDATE conversations SET last_chat_time = created_at WHERE last_chat_time = '' OR last_chat_time IS NULL"
        )

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

    # ============================================================
    # 消息历史
    # ============================================================

    def load_context(self, session_id: str, max_turns: int = None) -> list:
        if max_turns is None:
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
        try:
            h = json.dumps({"type": "human", "content": human_msg}, ensure_ascii=False)
            a = json.dumps({"type": "ai", "content": ai_msg}, ensure_ascii=False)
            self._conn.execute(
                "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                (session_id, h),
            )
            self._conn.execute(
                "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                (session_id, a),
            )
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self._conn.execute(
                "UPDATE conversations SET last_chat_time = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            self._conn.execute(
                "UPDATE conversations SET session_title = ? WHERE id = ? AND (session_title = '' OR session_title IS NULL)",
                (_truncate_title(human_msg), session_id),
            )
            self._conn.commit()
            return True
        except Exception as e:
            logger.error(f"【会话记忆】消息持久化失败: {e}")
            return False

    def _read_messages(self, session_id: str) -> list:
        rows = self._conn.execute(
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

    # ============================================================
    # 会话 CRUD
    # ============================================================

    def create_conversation(self, user_id: str, title: str = "") -> str:
        session_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._conn.execute(
            """INSERT INTO conversations
               (id, user_id, session_title, is_top, last_chat_time, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (session_id, user_id, _truncate_title(title) if title else "", now, now, now),
        )
        self._conn.commit()
        logger.debug(f"【会话记忆】创建会话: {session_id}")
        return session_id

    def get_user_conversations(self, user_id: str, offset: int = 0, limit: int | None = None) -> list[dict]:
        if limit is None:
            limit = int(get_config("pagination_default_limit", 20))
        cursor = self._conn.execute(
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

    def get_conversation_messages(self, session_id: str) -> list[dict]:
        rows = self._read_messages(session_id)
        return [{"role": msg_type, "content": content} for msg_type, content in rows]

    def toggle_pin(self, session_id: str, is_top: bool):
        self._conn.execute(
            "UPDATE conversations SET is_top = ? WHERE id = ?",
            (1 if is_top else 0, session_id),
        )
        self._conn.commit()

    def touch_conversation(self, session_id: str, first_query: str = ""):
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._conn.execute(
            "UPDATE conversations SET last_chat_time = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        if first_query:
            self._conn.execute(
                "UPDATE conversations SET session_title = ? WHERE id = ? AND (session_title = '' OR session_title IS NULL)",
                (_truncate_title(first_query), session_id),
            )
        self._conn.commit()

    def update_title(self, session_id: str, title: str):
        self._conn.execute(
            "UPDATE conversations SET session_title = ? WHERE id = ?",
            (_truncate_title(title), session_id),
        )
        self._conn.commit()

    def delete_conversation(self, session_id: str):
        try:
            self._conn.execute("BEGIN TRANSACTION")
            self._conn.execute("DELETE FROM message_store WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM conversations WHERE id = ?", (session_id,))
            self._conn.execute("COMMIT")
            logger.debug(f"【会话记忆】硬删除会话: {session_id}")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def clear_user(self, user_id: str):
        try:
            self._conn.execute("BEGIN TRANSACTION")
            sessions = self._conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchall()
            for (sid,) in sessions:
                self._conn.execute("DELETE FROM message_store WHERE session_id = ?", (sid,))
            self._conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            self._conn.execute("COMMIT")
            logger.info(f"【会话记忆】硬删除用户 {user_id} 全部会话")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
