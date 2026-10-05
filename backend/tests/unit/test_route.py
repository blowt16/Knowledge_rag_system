"""节点 2 route —— 三分类与两层收严（§7.2 M2-2 的测试点）。

两条最容易写错的规则（都会造成**直接答非所问**，且规则层没有 LLM 复核机会）：

1. **规则层只吃「明确的」** —— 不判 `clarify`、不做「短 → 闲聊」推断。
   按字面实现「短句判定」的话，「挂科了怎么办」会被判成闲聊，
   而 `chat` 分支明确**不检索、不引用**。
2. **`last_route` 只有 `knowledge` 参与粘性** —— 否则
   「你好」→「那这个要多久啊」被粘成 chat，同样不检索不引用。

⚠️ 兜底到 `knowledge` 时 `route_source` 必须记 **`llm`**（不是 `rule`）：
   它确实走过 LLM 调用、只是没拿到可用结果。记错会污染「规则命中率」的口径 ——
   那个指标的全部意义就是「规则层省下了多少次 LLM 调用」。
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.graph.nodes import route as RT
from app.graph.state import Message, new_state

# 够长（≥8）且不含制度名词/文号 → 规则层判不了、也不触发澄清，才会落到 LLM
VAGUE = "这个东西到底怎么弄才行"

HISTORY = [Message(role="user", content="转专业的申请条件是什么？"),
           Message(role="assistant", content="（答）")]


@pytest.fixture
def no_llm(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("规则层命中时不该调用 LLM")
    monkeypatch.setattr(llm, "complete_json", boom)


# ============================================================
# 规则层
# ============================================================

def test_rule_layer_only_classifies_the_obvious():
    assert RT.rule_classify("你好") == "chat"
    assert RT.rule_classify("桂电教〔2025〕28号") == "knowledge"
    assert RT.rule_classify("你好，我想问下缓考") == "knowledge"   # 优先级
    assert RT.rule_classify("缓考") == "knowledge"
    # 规则层不判 clarify —— 意图模糊正是规则判不了的
    assert RT.rule_classify("这个东西怎么弄") is None
    # 不做「短 → 闲聊」推断
    assert RT.rule_classify("挂科了怎么办") == "knowledge"


# ============================================================
# 澄清触发（仅两种）
# ============================================================

def test_clarify_trigger_one_unresolved_reference():
    s = new_state(query="那这个要多久啊", resolved_query="那这个要多久啊",
                  resolve_unresolved=True, history=HISTORY)
    assert RT._should_clarify(s) is True


def test_clarify_trigger_two_short_and_no_topic():
    s = new_state(query="怎么办", resolved_query="怎么办", history=HISTORY)
    assert RT._should_clarify(s) is True


def test_no_clarify_when_topic_is_locatable():
    """**误报是体验最差的失败模式** —— 能定位主题就绝不反问。"""
    for q in ["缓考", "转专业", "桂电教〔2025〕28号",
              "我想问一下本科生学业预警以及学业退学实施细则里关于退学的具体规定"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 不该触发澄清"


# ============================================================
# 节点行为（mock LLM）
# ============================================================

async def test_greeting_is_chat_without_llm(no_llm):
    out = await RT.route_node(new_state(query="你好", resolved_query="你好"))
    assert out["route"] == "chat"
    assert out["route_source"] == "rule"


async def test_priority_knowledge_over_chat(no_llm):
    """§7.2 M2-2 ② 原题：命中闸门词表，但整条消息**有实际诉求**。"""
    q = "你好，我想问下缓考"
    out = await RT.route_node(new_state(query=q, resolved_query=q))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "rule"


async def test_doc_number_is_knowledge_without_llm(no_llm):
    q = "桂电教〔2025〕28号"
    out = await RT.route_node(new_state(query=q, resolved_query=q))
    assert out["route"] == "knowledge"


async def test_last_route_chat_is_not_sticky(no_llm):
    """上轮 chat + 本轮指代无先行词 → **不得粘成 chat**（粘了就答非所问）。"""
    q = "那这个要多久啊"
    out = await RT.route_node(new_state(query=q, resolved_query=q,
                                        last_route="chat", history=HISTORY))
    assert out["route"] != "chat"
    assert out["route"] == "clarify"   # 无先行词 → 澄清


async def test_last_route_chat_but_resolved_to_a_real_question():
    """同样上轮 chat，但本轮已被消解成实义问题 → knowledge。"""
    async def fake(messages, **kw):
        return {"route": "knowledge"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(llm, "complete_json", fake)
    try:
        out = await RT.route_node(new_state(query="那这个要多久啊",
                                            resolved_query="缓考申请要提前多久",
                                            last_route="chat", history=HISTORY))
    finally:
        monkey.undo()
    assert out["route"] == "knowledge"


async def test_llm_failure_falls_back_to_knowledge_with_source_llm(monkeypatch):
    async def boom(messages, **kw):
        raise llm.LLMTimeout("boom")
    monkeypatch.setattr(llm, "complete_json", boom)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "llm", "兜底必须记 llm，否则规则命中率口径被污染"


async def test_illegal_llm_value_also_falls_back(monkeypatch):
    async def weird(messages, **kw):
        return {"route": "banana"}
    monkeypatch.setattr(llm, "complete_json", weird)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "llm"


async def test_llm_may_choose_chat_or_clarify(monkeypatch):
    async def fake(messages, **kw):
        return {"route": "chat"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "chat"
    assert out["route_source"] == "llm"


async def test_sticky_last_route_is_only_knowledge(monkeypatch):
    """送进提示词的「上一轮分类」只有 knowledge 是有效值，其余写「（无）」。"""
    seen: list[str] = []

    async def capture(messages, **kw):
        seen.append(messages[0]["content"])
        return {"route": "knowledge"}

    monkeypatch.setattr(llm, "complete_json", capture)

    for last, expect_sticky in [("knowledge", "knowledge"), ("chat", "（无）"),
                                ("clarify", "（无）"), ("", "（无）")]:
        await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                      last_route=last, history=HISTORY))

    for prompt, (last, expect_sticky) in zip(seen, [("knowledge", "knowledge"),
                                                   ("chat", "（无）"),
                                                   ("clarify", "（无）"), ("", "（无）")]):
        assert expect_sticky in prompt.split("【上一轮分类】")[1].split("【用户问题】")[0] \
            or expect_sticky in prompt, f"last_route={last!r} 的粘性写错了"


# ============================================================
# M2 首跑实测发现的缺口（2026-10-05）
# ============================================================

async def test_symbol_only_input_is_not_clarified(no_llm):
    """纯符号输入**不得触发澄清**（§15 #6）。

    ⚠️ 实测现场：题库 mt-13 的「？？？」原来被判成 clarify ——
       短、又没有制度名词，正好踩中澄清触发条件二。用户敲了三个问号，
       系统回一句「你想问的是哪一项？」，这是纯噪声。
       修法是把「没有实义字符」并入闸门判据（与 resolve 共用同一份判据）。
    """
    for q in ["？？？", "。。。", "—— !!!"]:
        out = await RT.route_node(new_state(query=q, resolved_query=q))
        assert out["route"] != "clarify", f"{q} 触发了无意义澄清"
        assert out["route"] == "chat"
