"""多轮题库的字段完整性（回归锁）。

⚠️ 为什么值得单独一条测试：题库是**手写的 JSON**，字段名写错不会报任何错 ——
   评测器读不到该字段就当成「不判」，指标会**悄悄变好看**（分母少了一批题）。
   M1 的 `eval_min20.json` 至今只靠人眼核对，没有任何测试盯着它。

⚠️ 这里只查**结构与取值域**，不查文档标题是否真实存在 ——
   后者要连库，在 `tests/integration/test_eval_fixture.py`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "eval_multiturn.json"
ROUTES = {"chat", "clarify", "knowledge"}


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_top_level_shape(payload):
    assert payload["version"] == 1
    assert payload["cases"], "题库为空 —— 评测会跑出 0 题却显示成功"
    assert payload["note"] and payload["caveat"]


def test_case_and_turn_required_fields(payload):
    ids = [c["id"] for c in payload["cases"]]
    assert len(ids) == len(set(ids)), "case id 重复"

    for case in payload["cases"]:
        assert case["type"] and case["note"], f"{case['id']} 缺 type/note"
        assert case["turns"], f"{case['id']} 没有轮次"
        for i, turn in enumerate(case["turns"], start=1):
            where = f"{case['id']} 第 {i} 轮"
            assert turn["question"].strip(), f"{where} question 为空"
            assert turn["expected_route"] in ROUTES, f"{where} expected_route 非法"
            assert turn["should_clarify"] in (0, 1), f"{where} should_clarify 必须是 0/1"
            # 两个键必须**存在**（值可以是 null）——「没写」和「明确不判」是两回事
            assert "expected_document" in turn, f"{where} 缺 expected_document 键"
            assert "resolved_must_contain_any" in turn, f"{where} 缺锚点键"
            if turn.get("probe"):
                assert turn["probe"] is True, f"{where} probe 只能为 true"


def test_clarify_and_chat_turns_have_no_retrieval_target(payload):
    """澄清轮与闲聊轮**不该有检索目标** —— 有就是标注写串行了。"""
    for case in payload["cases"]:
        for i, turn in enumerate(case["turns"], start=1):
            if turn["expected_route"] in ("chat", "clarify"):
                assert turn["should_clarify"] == (1 if turn["expected_route"] == "clarify" else 0), \
                    f"{case['id']} 第 {i} 轮：route={turn['expected_route']} 与 should_clarify 矛盾"
                assert turn["expected_document"] is None, \
                    f"{case['id']} 第 {i} 轮：{turn['expected_route']} 轮不该有 expected_document"


def test_multiturn_cases_have_followup(payload):
    """多轮题至少两轮 —— 一轮的「多轮题」测不出指代。"""
    for case in payload["cases"]:
        if case["type"] == "multi_turn":
            assert len(case["turns"]) >= 2, f"{case['id']} 只有一轮，测不了跨轮指代"


def test_anchor_turns_are_not_first(payload):
    """锚点断言只出现在**后续轮** —— 首轮是自足问题，不该要求消解。"""
    for case in payload["cases"]:
        for i, turn in enumerate(case["turns"], start=1):
            if turn["resolved_must_contain_any"] and i == 1:
                pytest.fail(f"{case['id']} 首轮就要求消解锚点 —— 首轮没有上下文可消解")
