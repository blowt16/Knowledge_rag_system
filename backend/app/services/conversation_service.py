"""会话与消息（§3.3.1 / §3.7.2）。

⚠️ **落库时机很重要**：**存原始答案（含 `[n]` 标记）+ 独立的 `citations` 列**。
   若在落库时就剥掉标记，用户重新打开会话时回答里既没角标也没引用，
   **与验收标准「引用可点击跳原文」冲突**。

   剥离只发生在**拼 `history` 时**（§3.8.4 第 ① 条）—— 入库是完整的，入 prompt 才是干净的。

⚠️ **组装与计数所用的 `messages` 一律是【不含本轮】的历史**（§3.8.5）：
   本轮用户消息**不落进** `messages[compressed_count:]`，
   只以 `[本轮问题 resolved_query]` 的形式出现在末尾 ——
   否则本轮问题会出现两次，且压缩触发时机与设计值不一致。

   `messages` 表在本轮**回答生成完成后**才写入，所以组装阶段读到的天然不含本轮。
"""

from __future__ import annotations

import re
import uuid

import asyncpg

from app import db
from app.core.config import cfg
from app.graph.state import Message

# 拼 history 时剥掉助手消息里的 [n] 标记（落库保留原文）
_MARKER_RE = re.compile(r"\[\d+\]")


async def create_conversation(session_id: str, user_id: str, *, title: str = "") -> None:
    """新会话。

    ⚠️ 会话创建只有一条路径：前端点「新建对话」只是清空本地 `session_id`，
       **不调 `POST /api/conversations`** —— 否则会话列表里会堆积没有消息的空会话。
       这里是问答流内部按需创建。
    """
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO conversations (id, user_id, title, last_chat_time)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (id) DO NOTHING""",
            session_id, user_id, (title or "新对话")[:50],
        )


async def load_history(session_id: str, *, limit: int | None = None) -> list[Message]:
    """加载历史。

    ⚠️ 截断口径以 §3.8.3 为准（token 预算 + 高/低水位）。M0 先用保留区条数占位；
       M3 换成 `count_tokens(摘要) + count_tokens(messages[compressed_count:]) > H`。

    ⚠️ 这里读到的天然**不含本轮** —— 本轮消息在回答生成完成后才写库。
    """
    keep = limit or int(cfg("context.recent_messages_limit", 10))
    async with db.tx() as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM messages
                WHERE conversation_id = $1
                ORDER BY created_at DESC LIMIT $2""",
            session_id, keep,
        )

    history: list[Message] = []
    for row in reversed(rows):
        content = row["content"] or ""
        if row["role"] == "assistant":
            # 剥掉 [n] 标记：上一轮答案带着 [1][2] 进历史，模型会模仿旧编号，
            # 而本轮证据可能只有 2 条，写出 [3] 立刻变成无效标记
            content = _MARKER_RE.sub("", content)
        history.append(Message(role=row["role"], content=content))
    return history


async def append_messages(conn: asyncpg.Connection, session_id: str, question: str,
                          answer: str, citations: list, route: str) -> None:
    """追加本轮问答。**必须在调用方的事务内、且在短锁保护下执行。**

    用户消息带 `route` —— 它是 `last_route` 的来源（§3.3.1）。
    """
    await conn.execute(
        """INSERT INTO messages (id, conversation_id, role, content, route)
           VALUES ($1, $2, 'user', $3, $4)""",
        uuid.uuid4().hex, session_id, question, route or None,
    )
    await conn.execute(
        """INSERT INTO messages (id, conversation_id, role, content, citations)
           VALUES ($1, $2, 'assistant', $3, $4)""",
        uuid.uuid4().hex, session_id, answer, citations or [],
    )


async def touch_conversation(conn: asyncpg.Connection, session_id: str) -> None:
    await conn.execute(
        "UPDATE conversations SET last_chat_time = now() WHERE id = $1", session_id
    )


async def list_conversations(user_id: str, *, offset: int = 0, limit: int = 20) -> dict:
    """会话列表。排序固定为 `is_top DESC, last_chat_time DESC`（§3.7.2）。

    软删除的会话不出现在列表里，**也不参与 total 计数**。
    """
    max_size = int(cfg("app.page_size_max", 100))
    limit = max(1, min(limit, max_size))
    async with db.tx() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM conversations WHERE user_id = $1 AND delete_flag = 0",
            user_id,
        )
        rows = await conn.fetch(
            """SELECT id, title, is_top, last_chat_time FROM conversations
                WHERE user_id = $1 AND delete_flag = 0
                ORDER BY is_top DESC, last_chat_time DESC
                OFFSET $2 LIMIT $3""",
            user_id, offset, limit,
        )
    items = [{
        "id": r["id"], "title": r["title"], "is_top": r["is_top"],
        "last_chat_time": r["last_chat_time"].isoformat() if r["last_chat_time"] else None,
    } for r in rows]
    return {
        "items": items,
        "total": total,
        # 两个都给：total 用于「共 N 条」，has_more 用于滚动到底加载下一页的判断
        "has_more": offset + len(items) < total,
    }


async def get_messages(session_id: str, user_id: str) -> list[dict]:
    """按时间**升序**，不分页（单会话消息量有界）。

    `citations` 是**落库的那一列** —— 用于刷新后重新渲染引用。
    注意 `verify_report` 只存在 `qa_logs`，所以重开历史会话时
    **无依据句的灰标不再显示**（引用角标仍在）—— 这是有意为之（§3.7.2）。
    """
    async with db.tx() as conn:
        owner = await conn.fetchval(
            "SELECT user_id FROM conversations WHERE id = $1 AND delete_flag = 0",
            session_id,
        )
        if owner != user_id:
            return []
        rows = await conn.fetch(
            """SELECT id, role, content, citations, created_at FROM messages
                WHERE conversation_id = $1 ORDER BY created_at ASC""",
            session_id,
        )
    return [{
        "id": r["id"], "role": r["role"], "content": r["content"],
        "citations": r["citations"] or [],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]
