"""滚动压缩的 DB 往返与 SSE 行为（§8.2 M3-1 的测试点 ③④）。

纯逻辑（水位线、并入条数、组装顺序）在 `tests/unit/test_token_budget.py`；
这里考的是**真库上的往返**与**用户视角的硬约束**：

- 压完两列真的落库了，且「摘要 + 尾部」拼得起来
- 摘要模型超时 → **本轮照常作答**（绝不变用户 500）
- A 触发（历史超 16K）时**绝不碰检索上下文**，也不删任何消息
- 一轮问答的两条消息顺序是「先问后答」（now() 是事务时刻，两条会撞在一起）
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from app import db
from app.core import llm
from app.core.deps import UserContext
from app.graph.state import Message
from app.services import chat_service
from app.services import context_service as CS
from app.services.conversation_service import append_messages, create_conversation
from tests.support import delete_admin, insert_admin


@pytest.fixture(autouse=True)
def _chars_as_tokens(monkeypatch):
    """1 字 = 1 token —— 预算可以按字数精确断言，不必真造 18,000 字。"""
    monkeypatch.setattr(CS, "count_tokens", lambda text: len(text or ""))


def _summarizer(value: str = "摘要"):
    async def fake(previous, merged):
        return value

    return fake


@pytest_asyncio.fixture
async def session():
    """一个干净的会话：14 轮 = 28 条消息，每条 500 字 → 14,000 字 > H(12,800)。

    ⚠️ **一轮一个事务** —— 生产路径就是这样（每轮问答各自加锁落库）。
       一个事务里塞多轮的话，`now()` 会让所有轮次撞在同一个时间戳上，
       那是测试构造出来的假象，不是真实数据形态。
    """
    admin_id = await insert_admin()
    sid = f"t_{uuid.uuid4().hex[:12]}"
    await create_conversation(sid, admin_id, title="压缩测试")
    for _ in range(14):
        async with db.tx() as conn:
            await append_messages(conn, sid, question="问" * 500, answer="答" * 500,
                                  citations=[], route="knowledge")
    try:
        yield sid, admin_id
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM degradation_events WHERE session_id = $1", sid)
            await conn.execute("DELETE FROM qa_logs WHERE session_id = $1", sid)
            await conn.execute("DELETE FROM messages WHERE conversation_id = $1", sid)
            await conn.execute("DELETE FROM conversations WHERE id = $1", sid)
        await delete_admin(admin_id)


async def _conversation_row(sid: str) -> dict:
    async with db.tx() as conn:
        return dict(await conn.fetchrow(
            "SELECT compressed_summary, compressed_count FROM conversations WHERE id = $1", sid
        ))


async def _message_count(sid: str) -> int:
    async with db.tx() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM messages WHERE conversation_id = $1", sid)


# ============================================================
# 往返
# ============================================================

async def test_load_context_compacts_and_persists(session, monkeypatch):
    sid, _ = session
    monkeypatch.setattr(CS, "_summarize", _summarizer("摘要"))

    slice_ = await CS.load_context(sid)

    assert slice_.compacted == 14
    assert slice_.degraded is None
    assert slice_.summary == "摘要"
    assert len(slice_.messages) == 14, "压掉 14 条，剩 14 条原文"
    row = await _conversation_row(sid)
    assert row["compressed_summary"] == "摘要"
    assert row["compressed_count"] == 14


async def test_second_compaction_continues_from_where_it_stopped(session, monkeypatch):
    """压缩是**增量**的：第二次从上次的 compressed_count 往后接着并。"""
    sid, _ = session
    monkeypatch.setattr(CS, "_summarize", _summarizer("摘要"))

    first = await CS.load_context(sid)
    assert first.compacted == 14

    # 再攒 12 条（6 轮 × 1,000 字）→ 7,002 + 6,000 > H → 第二次压缩
    async with db.tx() as conn:
        for _ in range(6):
            await append_messages(conn, sid, question="问" * 500, answer="答" * 500,
                                  citations=[], route="knowledge")

    second = await CS.load_context(sid)

    assert second.compacted == 12, "从上次停下的地方接着并"
    row = await _conversation_row(sid)
    assert row["compressed_count"] == 14 + 12
    assert row["compressed_summary"] == "摘要"


async def test_below_high_water_does_nothing(session, monkeypatch):
    """没超水位就**一条都不压** —— 滞回的意义就是别每轮都调摘要模型。"""
    sid, _ = session
    called = {"n": 0}

    async def fake(previous, merged):
        called["n"] += 1
        return "摘要"

    monkeypatch.setattr(CS, "_summarize", fake)
    async with db.tx() as conn:
        await conn.execute("DELETE FROM messages WHERE conversation_id = $1", sid)
        for _ in range(4):
            await append_messages(conn, sid, question="问" * 50, answer="答" * 50,
                                  citations=[], route="knowledge")

    slice_ = await CS.load_context(sid)

    assert (slice_.compacted, called["n"]) == (0, 0)
    assert len(slice_.messages) == 8


# ============================================================
# ③ 摘要模型坏了，本轮照常作答（硬约束）
# ============================================================

async def test_summary_failure_falls_back_and_records_degradation(session, monkeypatch):
    sid, _ = session

    async def boom(messages, **kwargs):
        raise llm.LLMTimeout("摘要模型超时")

    monkeypatch.setattr(llm, "complete", boom)

    slice_ = await CS.load_context(sid)

    assert slice_.degraded == "timeout"
    assert slice_.compacted == 0
    assert len(slice_.messages) == 28, "退回**未压缩**历史，一条不少"
    row = await _conversation_row(sid)
    assert (row["compressed_summary"], row["compressed_count"]) == (None, 0), "没压就不该写库"

    async with db.tx() as conn:
        kinds = await conn.fetch(
            "SELECT kind FROM degradation_events WHERE session_id = $1 AND node = 'compaction'",
            sid)
    assert [r["kind"] for r in kinds] == ["timeout"]


async def test_empty_summary_is_treated_as_failure_not_written_back(session, monkeypatch):
    """★ 评审抓出：摘要模型返回**空串**时不能当成压缩成功。

    `llm.complete` 对空 content 不抛异常 —— 不显式检查就会走「成功」分支：
    `compressed_summary` 被写成空串、`compressed_count` 照推，
    那 N 条消息**永久消失**（库里只有一列摘要，旧摘要也被就地销毁），
    用户侧表现是「助手突然忘了前面聊过什么」，且不可恢复。
    """
    sid, _ = session

    async def empty(messages, **kwargs):
        return ""

    monkeypatch.setattr(llm, "complete", empty)

    slice_ = await CS.load_context(sid)

    assert slice_.degraded == "unavailable", "空摘要必须记降级"
    assert slice_.compacted == 0
    assert len(slice_.messages) == 28, "一条都不能丢"
    row = await _conversation_row(sid)
    assert (row["compressed_summary"], row["compressed_count"]) == (None, 0), (
        "空摘要被回写了 —— 那批消息就再也回不来了"
    )
    async with db.tx() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM degradation_events WHERE session_id = $1 AND node = 'compaction'",
            sid)
    assert n == 1, "这条路径必须留痕，否则评测护栏看不见"


async def test_chat_still_answers_when_compaction_fails(session, monkeypatch):
    """★ 硬约束：摘要模型的故障**绝不能变成用户侧的 500**。"""
    sid, _ = session

    async def boom(messages, **kwargs):
        raise llm.LLMTimeout("摘要模型超时")

    monkeypatch.setattr(llm, "complete", boom)
    monkeypatch.setattr(chat_service, "get_graph", lambda: _AnswerGraph())

    events, frames = await _run_stream(sid)

    assert "error" not in events, "压缩失败不该让这一轮报错"
    assert "done" in events
    assert dict(frames)["token"]["text"] == "正常答案[1]。"


# ============================================================
# ④ A 触发时绝不动检索上下文
# ============================================================

async def test_compaction_never_touches_retrieval_state(session, monkeypatch):
    """A 触发只压历史：消息一条不删，检索上下文（还没产生）更碰不到。

    这条对应 §3.8.2 的硬约束 —— 「历史一超 16K 就裁证据」是把方向做反了：
    检索到的是本轮**回答依据**，历史只是理解指代的背景。
    """
    sid, _ = session
    monkeypatch.setattr(CS, "_summarize", _summarizer("摘要"))
    graph = _AnswerGraph()
    monkeypatch.setattr(chat_service, "get_graph", lambda: graph)
    before = await _message_count(sid)

    await _run_stream(sid)

    assert graph.state["summary"] == "摘要"
    assert len(graph.state["history"]) == 14, "压过的历史进了图"
    # 检索产出的字段：压缩阶段根本不认识它们
    assert graph.state["evidence"] == []
    assert graph.state["reranked"] == []
    # 压缩不删任何历史消息 —— 只是换个方式喂给模型（+2 是本轮问答刚落的库）
    assert await _message_count(sid) == before + 2


async def test_current_turn_is_not_part_of_the_history(session, monkeypatch):
    """组装与计数用的 messages **不含本轮**（§3.8.5）—— 本轮以问题形式在末尾。"""
    sid, _ = session
    monkeypatch.setattr(CS, "_summarize", _summarizer("摘要"))
    graph = _AnswerGraph()
    monkeypatch.setattr(chat_service, "get_graph", lambda: graph)

    await _run_stream(sid, query="本轮要问的问题")

    assert all("本轮要问的问题" not in m.content for m in graph.state["history"])
    assert graph.state["query"] == "本轮要问的问题"


# ============================================================
# 消息顺序（now() 是事务时刻，两条会撞在同一个时间戳上）
# ============================================================

async def test_turn_messages_are_ordered_question_then_answer(session, monkeypatch):
    """一轮问答的两条消息必须**先问后答**地读出来。

    实测：同一个事务里 `now()` 取到的是**同一个**时间戳，只按 created_at
    排序的话，历史可能以「助手在用户之前」的形式进提示词。

    ⚠️ 这里必须桩掉摘要模型：本卷历史本来就超水位，不桩的话
       **测试会真的去调一次线上模型**（写这条时踩过一次）。
    """
    monkeypatch.setattr(CS, "_summarize", _summarizer("摘要"))
    sid, _ = session

    slice_ = await CS.load_context(sid)

    roles = [m.role for m in slice_.messages]
    assert roles[:2] == ["user", "assistant"]
    assert slice_.messages[0].content.startswith("问")
    assert slice_.messages[1].content.startswith("答")
    # 整卷都要成对（压缩只从最老的那端整段并入，不会打散配对）
    assert roles == ["user", "assistant"] * (len(roles) // 2)


# ---- 假图与 SSE 解析 -------------------------------------------------------

class _AnswerGraph:
    """记录初始 state，然后回放一个正常答案。"""

    def __init__(self):
        self.state: dict = {}

    async def astream(self, state, stream_mode=None):
        self.state = dict(state)
        yield ("values", {
            "route": "knowledge",
            "answer": "正常答案[1]。",
            "decision": "ANSWERED",
            "citations": [],
            "trace": [{"node": "route", "ms": 1, "recalled": 0, "degraded": None},
                      {"node": "generate", "ms": 1, "recalled": 1, "degraded": None}],
        })


async def _run_stream(sid: str, *, query: str = "缓考怎么申请？"):
    frames: list[tuple[str, dict]] = []
    stream = chat_service.stream_chat(
        query=query, session_id=sid, is_new_session=False, include_restricted=False,
        user=UserContext(id="u", username="u", role="student", token_version=0),
        is_disconnected=_never_disconnected,
    )
    async for frame in stream:
        event = frame.split("\n", 1)[0].removeprefix("event: ")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        frames.append((event, data))
    return [e for e, _ in frames], frames


async def _never_disconnected() -> bool:
    return False
