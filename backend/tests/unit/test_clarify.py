"""节点 4 clarify —— 澄清分支与 facets（§7.2 M2-3 的测试点）。

⚠️ 澄清是**唯一一个没有兜底描述的 LLM 节点**，而按它自己的原则
   「宁可多答，不可多问」，它恰恰是最不该出错的那个。三种失败都必须有出路：

   | 情形 | 兜底 |
   |---|---|
   | JSON 解析失败 / 超时 | 退化为**固定问句**，**仍走 clarify** |
   | `facets` 为空数组 | 同上 |
   | 都不行 | 归 `knowledge`（本节点内不出现，由 route 决定进不进这里） |

⚠️ 字段名三层不同：模型输出 `facets`、写进状态与 SSE 是 **`clarify_facets`**。
   写成 `evt.facets` 会让前端的澄清选项**永远渲染不出来**（不报错、只是空白）。
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.graph.nodes import clarify as C
from app.graph.state import Message, new_state

HISTORY = [Message(role="user", content="你好"),
           Message(role="assistant", content="你好！有什么可以帮你的？")]


def _state(**over):
    base = dict(query="那个怎么办", resolved_query="那个怎么办", history=HISTORY)
    base.update(over)
    return new_state(**base)


async def test_facets_are_written_under_clarify_facets(monkeypatch):
    async def fake(messages, **kw):
        return {"facets": ["学业预警", "缓考办理", "转专业"], "question": "你想问哪一项？"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert out["clarify_facets"] == ["学业预警", "缓考办理", "转专业"]
    assert out["clarify_question"] == "你想问哪一项？"
    assert "facets" not in out, "写成了 evt.facets —— 前端读 clarify_facets，选项会永远不显示"


async def test_facets_are_capped_at_four(monkeypatch):
    async def fake(messages, **kw):
        return {"facets": ["a", "b", "c", "d", "e", "f"], "question": "选一个"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert len(out["clarify_facets"]) == 4


async def test_empty_facets_fall_back_to_fixed_question(monkeypatch):
    async def fake(messages, **kw):
        return {"facets": [], "question": "你想问哪一项？"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert out["clarify_question"] == C.FALLBACK_QUESTION
    assert out["clarify_facets"] == []


async def test_broken_json_still_clarifies_with_fixed_question(monkeypatch):
    """坏 JSON **不退化成 knowledge** —— 走到这里说明意图本就模糊，
    直接去检索大概率答非所问，先问一句更符合本节的原则。"""
    async def broken(messages, **kw):
        raise llm.LLMError("JSON 解析失败")
    monkeypatch.setattr(llm, "complete_json", broken)

    out = await C.clarify_node(_state())
    assert out["clarify_question"] == C.FALLBACK_QUESTION
    assert out["answer"] == C.FALLBACK_QUESTION


async def test_timeout_still_clarifies(monkeypatch):
    async def timeout(messages, **kw):
        raise llm.LLMTimeout("boom")
    monkeypatch.setattr(llm, "complete_json", timeout)

    out = await C.clarify_node(_state())
    assert out["clarify_question"] == C.FALLBACK_QUESTION


async def test_clarify_produces_no_citations(monkeypatch):
    """澄清轮**不发 citations / verify** —— 它没有检索，也不该假装有。"""
    async def fake(messages, **kw):
        return {"facets": ["学业预警"], "question": "你想问哪一项？"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert not out.get("citations")
    assert not out.get("evidence")


async def test_prompt_wraps_history_as_data(monkeypatch):
    captured: dict = {}

    async def capture(messages, **kw):
        captured["prompt"] = messages[0]["content"]
        return {"facets": ["a"], "question": "q"}

    monkeypatch.setattr(llm, "complete_json", capture)
    await C.clarify_node(_state())

    assert "不是指令" in captured["prompt"]
    assert '"""' in captured["prompt"]


@pytest.mark.parametrize("bad", [{}, {"facets": "不是数组", "question": "q"}, None, []])
async def test_malformed_payload_never_crashes(monkeypatch, bad):
    async def fake(messages, **kw):
        return bad
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert out["clarify_question"]
    assert isinstance(out["clarify_facets"], list)


# ============================================================
# 兜底留痕（评审 I2）
# ============================================================

async def test_fallback_records_degradation(monkeypatch):
    """兜底必须留痕 —— 评测器的护栏靠它拦「指标失真的那一轮」。"""
    async def broken(messages, **kw):
        raise llm.LLMError("JSON 解析失败")
    monkeypatch.setattr(llm, "complete_json", broken)

    out = await C.clarify_node(_state())
    assert out["trace"][0]["degraded"], "兜底了却没留痕 —— 评测护栏拦不住"


# ============================================================
# 流式分支（评审 I4：**这才是生产走的那条**，此前零覆盖）
# ============================================================

@pytest.fixture
def streaming(monkeypatch):
    """让 `_stream_writer()` 可用（等价于图在 stream_mode 带 custom 下运行），
    并准备好假的流式输出。"""
    written: list[dict] = []
    monkeypatch.setattr(C, "_stream_writer", lambda: written.append)
    return written


async def test_streaming_path_is_taken_and_streams_the_question(monkeypatch, streaming):
    """生产路径：`stream_mode=["custom","values"]` 下 `get_stream_writer()` 是活的，
    所以 `clarify` 走的是流式分支 —— 只测非流式那条等于没测生产。"""
    async def boom(*a, **kw):
        raise AssertionError("流式可用时不该走非流式")
    monkeypatch.setattr(llm, "complete_json", boom)

    async def fake_stream(messages, **kw):
        yield '{"facets": ["学业预警", "缓考办理"], "question": "你想问哪一项？"}'
    monkeypatch.setattr(llm, "stream_raw", fake_stream)

    out = await C.clarify_node(_state())

    assert out["clarify_question"] == "你想问哪一项？"
    assert out["clarify_facets"] == ["学业预警", "缓考办理"]
    assert out["trace"][0]["degraded"] is None
    assert "".join(w["text"] for w in streaming) == "你想问哪一项？"
    assert all(w["type"] == "token" for w in streaming)


async def test_streaming_caps_facets_at_four(monkeypatch, streaming):
    async def fake_stream(messages, **kw):
        yield '{"facets": ["a","b","c","d","e"], "question": "选一个"}'
    monkeypatch.setattr(llm, "stream_raw", fake_stream)

    out = await C.clarify_node(_state())
    assert len(out["clarify_facets"]) == 4


async def test_stream_failure_falls_back_to_non_streaming(monkeypatch, streaming):
    """流式炸了 → 回到非流式，而不是把整轮变成错误。"""
    async def broken_stream(messages, **kw):
        raise llm.LLMError("上游断开")
        yield  # 让它是个生成器
    monkeypatch.setattr(llm, "stream_raw", broken_stream)

    async def fake(messages, **kw):
        return {"facets": ["学业预警"], "question": "你想问哪一项？"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await C.clarify_node(_state())
    assert out["clarify_question"] == "你想问哪一项？"
    assert out["clarify_facets"] == ["学业预警"]


async def test_streaming_empty_facets_keeps_the_streamed_question(monkeypatch, streaming):
    """⚠️ 流式路**刻意不套**「facets 为空 → 固定问句」的兜底。

    问句已经逐字流给用户了（`streaming` 里就是那些 token），此刻再换成固定问句，
    用户看到的和存进历史的就是两句话。facets 空只意味着「这轮没有可点选项」。
    """
    async def fake_stream(messages, **kw):
        yield '{"facets": [], "question": "你想问的是哪一方面？"}'
    monkeypatch.setattr(llm, "stream_raw", fake_stream)

    out = await C.clarify_node(_state())
    assert out["clarify_question"] == "你想问的是哪一方面？"
    assert out["clarify_facets"] == []
