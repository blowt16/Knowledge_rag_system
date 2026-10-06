"""澄清轮次上限（负责人 2026-10-06 定的规则）。

**要解决的问题**：短问题（<8 字）且未命中制度名词时会触发澄清反问，
而反问给出的 facet 往往**也是短句** —— 用户点一下，又被问一次。
实测（E2E）：问「食堂几点开门？」被反问，点它给的选项又被反问。

**规则**（负责人定）：
- **单链**最多 2 轮：同一条追问链里连续反问不超过 2 次
- **会话总计**最多 6 次
- 超限就**不再反问**，改为「按最可能的理解作答 + 一句说明 + 引导重新提问」

⚠️ 计数口径（关键）：
- 「链」= **紧邻当前这轮之前**、连续被路由为 `clarify` 的用户消息条数。
  中间只要有一轮不是澄清，链就断了（换话题 = 新链）。
- 「总计」= 本会话历史上一共澄清过多少次。
- 两者都从 `messages` 里数（`route` 落在用户消息那一行），
  所以「最多 2 轮」的含义是：**第 3 次该澄清时被拦下**。
"""

from __future__ import annotations

import pytest

from app.graph.nodes.route import _clarify_limit_reached, route_node


def _state(**over) -> dict:
    base = {"query": "食堂几点开门？", "clarify_chain": 0, "clarify_total": 0}
    base.update(over)
    return base


# ---------------------------------------------------------------- 判据

@pytest.mark.parametrize("chain,total,expected", [
    (0, 0, False),      # 还没澄清过 —— 正常反问
    (1, 1, False),      # 第 2 轮 —— 还在上限内
    (2, 2, True),       # 第 3 轮 —— 单链到顶
    (0, 6, True),       # 链是新的，但会话总次数到顶
    (2, 6, True),
    (1, 5, False),      # 都差一次 —— 还能再问一轮
])
def test_limit_judgement(chain, total, expected):
    assert _clarify_limit_reached(_state(clarify_chain=chain,
                                         clarify_total=total)) is expected


# ---------------------------------------------------------------- 路由行为

async def test_under_limit_still_clarifies():
    """没到上限时**照旧反问** —— 不能因为加了上限就把澄清废掉。"""
    result = await route_node(_state(clarify_chain=1, clarify_total=1))

    assert result["route"] == "clarify"
    assert not result.get("clarify_skipped")


async def test_chain_limit_answers_instead_of_asking():
    """★ 单链到顶：不再反问，按最可能的理解去作答（route=knowledge）。"""
    result = await route_node(_state(clarify_chain=2, clarify_total=2))

    assert result["route"] == "knowledge", "到顶还在反问 —— 用户会被反复追问"
    assert result["clarify_skipped"] is True, \
        "必须留下「跳过澄清」的痕，否则前端无从提示「我理解你想问的是…」"


async def test_session_limit_answers_instead_of_asking():
    """★ 会话总次数到顶：即使是新链也不再反问。"""
    result = await route_node(_state(clarify_chain=0, clarify_total=6))

    assert result["route"] == "knowledge"
    assert result["clarify_skipped"] is True


async def test_non_clarify_question_is_unaffected():
    """本来就该走 knowledge 的问题，不该被这套逻辑碰到。"""
    result = await route_node(_state(query="转专业需要满足什么条件？",
                                     clarify_chain=2, clarify_total=6))

    assert result["route"] == "knowledge"
    assert not result.get("clarify_skipped"), \
        "没触发澄清条件的问题，不该被标成「跳过澄清」"


async def test_meaningless_message_still_chat():
    """闸门优先于澄清与上限：打招呼仍是 chat（M2 实测踩过的坑）。"""
    result = await route_node(_state(query="你好", clarify_chain=2, clarify_total=6))

    assert result["route"] == "chat"
    assert not result.get("clarify_skipped")
