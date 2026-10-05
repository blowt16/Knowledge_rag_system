"""上下文预算与滚动压缩 —— 纯逻辑部分（§8.2 M3-1 的测试点）。

DB 往返与 SSE 层的行为在 `tests/integration/test_compaction.py`。

这里的用例把 token 计数换成**字数**（monkeypatch），预算就能精确断言 ——
否则每个用例都得先造 18,000 字的中文，还依赖 dashscope 分词器的具体切法。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core import llm
from app.core import prompts
from app.graph.state import Message
from app.services import context_service as CS


@pytest.fixture(autouse=True)
def _chars_as_tokens(monkeypatch):
    """1 字 = 1 token，让水位线可以直接用字数算。"""
    monkeypatch.setattr(CS, "count_tokens", lambda text: len(text or ""))


def _messages(count: int, chars: int) -> list[Message]:
    return [Message(role="user" if i % 2 == 0 else "assistant", content="甲" * chars)
            for i in range(count)]


def _summarizer(value: str = "摘要"):
    """把 `_summarize` 换成固定返回值的假实现（记录每次并入的条数）。"""
    calls: list[int] = []

    async def fake(previous, merged):
        calls.append(len(merged))
        return value

    fake.calls = calls          # type: ignore[attr-defined]
    return fake


# ============================================================
# 水位线（§3.8.2 / §3.8.3）
# ============================================================

def test_water_marks_come_from_the_configured_budget():
    """H = 16,000 × 80% = 12,800；L = 16,000 × 50% = 8,000（§3.8.3）。"""
    assert CS._history_budget() == 16000
    assert CS._high_water() == 12800
    assert CS._low_water() == 8000
    assert CS._reserve() == 10
    assert CS._min_merge() == 6
    assert CS._summary_cap() == 800


def test_hard_limit_leaves_room_for_output_and_margin():
    """硬上限 = 窗口 − 最大输出 − 安全余量（§3.8.2）。"""
    assert CS.hard_prompt_limit() == 1048576 - 8192 - 500


def test_trigger_counts_the_summary_too():
    """★ 判据是「**摘要 + 未压缩消息**」—— 摘要不计入的话压缩永不收敛。"""
    assert CS.history_tokens("甲" * 100, []) == 100
    assert CS.history_tokens("", _messages(2, 10)) == 20
    assert CS.history_tokens("甲" * 100, _messages(2, 10)) == 120


# ============================================================
# 压缩执行量（§3.8.3）
# ============================================================

async def test_merges_oldest_until_below_low_water(monkeypatch):
    """从**最老**的并入，直到剩余历史 ≤ L；一次压缩只调一次摘要模型。"""
    fake = _summarizer()
    monkeypatch.setattr(CS, "_summarize", fake)
    messages = _messages(24, 500)          # 12,000 字 > H

    summary, merged = await CS._compact("", messages)

    assert merged == 10, "并入 10 条后剩 7,000 字 ≤ L − 摘要上限(800)"
    assert fake.calls == [10], "一次压缩只调一次摘要模型（滞回的意义就在这里）"
    assert summary == "摘要"


async def test_merges_at_least_min_messages(monkeypatch):
    """单次至少并入 6 条 —— 否则每轮都要调一次摘要模型。"""
    fake = _summarizer()
    monkeypatch.setattr(CS, "_summarize", fake)
    messages = _messages(16, 5)            # 80 字，远没超 L，但压缩区有 6 条

    _, merged = await CS._compact("", messages)

    assert merged == 6


async def test_never_touches_the_reserve_region(monkeypatch):
    """保留区（最近 10 条）原文**永不动** —— 压到只剩保留区就停手。"""
    fake = _summarizer()
    monkeypatch.setattr(CS, "_summarize", fake)
    messages = _messages(12, 5000)         # 6 万字，怎么压都不够

    _, merged = await CS._compact("", messages)

    assert merged == 2, "只剩 2 条可压（12 − 保留区 10），压完就停，不许碰保留区"


async def test_no_compaction_when_only_reserve_is_left(monkeypatch):
    fake = _summarizer()
    monkeypatch.setattr(CS, "_summarize", fake)
    summary, merged = await CS._compact("旧摘要", _messages(8, 10))
    assert (summary, merged) == ("旧摘要", 0)
    assert fake.calls == []


async def test_force_compact_merges_everything_compressible(monkeypatch):
    """强制压缩（溢出兜底的第二步）**不看水位线**，能压多少压多少。"""
    monkeypatch.setattr(CS, "_summarize", _summarizer())
    messages = _messages(20, 10)

    _, merged = await CS._compact("", messages, force=True)

    assert merged == 10


# ============================================================
# 摘要调用本身
# ============================================================

async def test_summary_call_is_capped_and_uses_its_own_timeout(monkeypatch):
    """★ 摘要自身上限 800 token —— 否则它会一轮比一轮胖，把窗口撑爆。"""
    captured: dict = {}

    async def fake_complete(messages, **kwargs):
        captured.update(kwargs)
        captured["prompt"] = messages[0]["content"]
        return "  摘要正文  "

    monkeypatch.setattr(llm, "complete", fake_complete)

    text = await CS._summarize("旧摘要", _messages(2, 5))

    assert text == "摘要正文"
    assert captured["max_tokens"] == 800
    assert captured["timeout"] == 20
    assert "旧摘要" in captured["prompt"]
    assert "不是指令" in captured["prompt"], "压缩也要防注入 —— 它吃的是原始对话"


def test_compaction_strips_reference_markers():
    """拼进摘要的对话要剥掉 `[n]`（§3.8.4 防线①），否则模型会模仿旧编号。"""
    text = CS._format([Message(role="assistant", content="答案是……[1][2]。")])
    assert text == "助手：答案是……。"


# ============================================================
# 组装顺序（§3.8.5）
# ============================================================

def test_prompt_assembly_order_is_system_summary_history_context_question():
    """[系统][摘要][messages[compressed_count:]][检索上下文][本轮问题] —— 顺序不能乱。

    越靠后的信息对模型的即时影响越大（近因效应），本轮问题必须压轴。
    """
    text = prompts.render(
        "generate", summary="<摘要>", history="<历史>",
        context="<证据>", query="<本轮问题>",
    )
    positions = [text.index(m) for m in ("<摘要>", "<历史>", "<证据>", "<本轮问题>")]
    assert positions == sorted(positions)


# ============================================================
# 写回
# ============================================================

async def test_write_back_updates_both_columns_in_one_statement(monkeypatch):
    """★ 两个字段**同一事务、同一条语句**写 —— 只写一个会拼不出完整历史。

    拆成两条 UPDATE 的话，中途崩溃就会留下「摘要覆盖了 A 段、
    compressed_count 还停在原处」的错位状态。
    """
    recorded: list[tuple[str, tuple]] = []

    class _Conn:
        async def execute(self, sql, *args):
            recorded.append((sql, args))

    @asynccontextmanager
    async def fake_tx():
        yield _Conn()

    monkeypatch.setattr(CS, "db", SimpleNamespace(tx=fake_tx))

    await CS._write_back("sid", "摘要", 12)

    assert len(recorded) == 1, "必须一条语句写完两列"
    sql, args = recorded[0]
    assert "compressed_summary" in sql and "compressed_count" in sql
    assert args == ("sid", "摘要", 12)
