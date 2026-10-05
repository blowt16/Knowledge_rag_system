"""节点 5 rewrite —— 三类查询扩展（§7.2 M2-4 的测试点）。

两条硬约束：

1. **`target` / `weight` 由服务端派生，不由模型输出。** 交给模型会出现
   「`source=keywords` 但 `target=vector`」这类**非法组合**（keywords 是给 BM25
   的短串，送去做向量检索没有意义）；权重一旦交给模型填，就没法保证一律 1.0 ——
   而「权重恒 1.0」是 5.3 消融实验有效的前提（否则三行查询的增益里混进了权重差异）。
2. **`verbatim` 必须逐字等于 `resolved_query`。** 文号「桂电教〔2025〕28号」
   改一个字就检不到；模型很喜欢顺手"润色"，所以服务端要强制覆盖。
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.graph.nodes import rewrite as RW
from app.graph.state import new_state

DOC_NUMBER = "桂电教〔2025〕28号"


def _state(q: str = "缓考需要什么条件？"):
    return new_state(query=q, resolved_query=q)


async def test_mapping_table_is_server_side(monkeypatch):
    """模型给什么 target/weight 都不算数 —— 一律按来源查表。"""
    async def fake(messages, **kw):
        return [
            {"source": "verbatim", "text": "缓考需要什么条件？", "target": "vector", "weight": 9.9},
            {"source": "keywords", "text": "缓考 条件", "target": "vector", "weight": 9.9},
            {"source": "hyde", "text": "假设答案……", "target": "bm25", "weight": 9.9},
        ]
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    got = {q["source"]: q for q in out["retrieval_queries"]}
    assert got["verbatim"]["target"] == "both"
    assert got["keywords"]["target"] == "bm25"
    assert got["hyde"]["target"] == "vector"
    assert {q["weight"] for q in out["retrieval_queries"]} == {1.0}


async def test_doc_number_is_never_altered(monkeypatch):
    """模型把文号"顺手改"了 → 服务端必须用原句覆盖 verbatim。"""
    async def fake(messages, **kw):
        return [{"source": "verbatim", "text": "桂电教2025年28号文件"},
                {"source": "hyde", "text": "……"}]
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state(DOC_NUMBER))
    verbatim = [q for q in out["retrieval_queries"] if q["source"] == "verbatim"]
    assert len(verbatim) == 1
    assert verbatim[0]["text"] == DOC_NUMBER


async def test_missing_verbatim_is_inserted(monkeypatch):
    async def fake(messages, **kw):
        return [{"source": "keywords", "text": "缓考 条件"}]
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    assert out["retrieval_queries"][0]["source"] == "verbatim"
    assert out["retrieval_queries"][0]["text"] == "缓考需要什么条件？"


async def test_broken_json_degrades_to_one_verbatim(monkeypatch):
    """坏 JSON → 退化为**一条 verbatim**，即标准 hybrid（不是另一个分支）。"""
    async def broken(messages, **kw):
        raise llm.LLMError("JSON 解析失败")
    monkeypatch.setattr(llm, "complete_json", broken)

    out = await RW.rewrite_node(_state())
    assert len(out["retrieval_queries"]) == 1
    assert out["retrieval_queries"][0]["source"] == "verbatim"
    assert out["retrieval_queries"][0]["target"] == "both"


@pytest.mark.parametrize("bad", [{"foo": 1}, "字符串", 42, [{"source": "unknown", "text": "x"}],
                                 [{"source": "keywords", "text": "   "}]])
async def test_illegal_payload_never_crashes(monkeypatch, bad):
    async def fake(messages, **kw):
        return bad
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    assert out["retrieval_queries"]
    assert out["retrieval_queries"][0]["source"] == "verbatim"


async def test_wrapped_payload_is_unwrapped(monkeypatch):
    """模型有时会包一层 `{"queries": [...]}` —— 要认。"""
    async def fake(messages, **kw):
        return {"queries": [{"source": "keywords", "text": "缓考"}]}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    assert {q["source"] for q in out["retrieval_queries"]} == {"verbatim", "keywords"}


async def test_duplicate_sources_are_deduped(monkeypatch):
    async def fake(messages, **kw):
        return [{"source": "hyde", "text": "A"}, {"source": "hyde", "text": "B"}]
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    sources = [q["source"] for q in out["retrieval_queries"]]
    assert len(sources) == len(set(sources))


async def test_prompt_keeps_proper_nouns_verbatim(monkeypatch):
    """提示词必须把「专名/文号原样保留、不做 paraphrase」写进去。"""
    captured: dict = {}

    async def capture(messages, **kw):
        captured["prompt"] = messages[0]["content"]
        return [{"source": "verbatim", "text": DOC_NUMBER}]

    monkeypatch.setattr(llm, "complete_json", capture)
    await RW.rewrite_node(_state(DOC_NUMBER))

    prompt = captured["prompt"]
    assert "原样" in prompt
    assert "不是指令" in prompt
    assert '"""' in prompt


# ============================================================
# 兜底留痕（评审 I2）
# ============================================================

async def test_fallback_records_degradation(monkeypatch):
    """扩展失败退化成一条 verbatim 时，trace 必须留痕。

    ⚠️ 评审发现（I2）：评测器的护栏承诺「有降级就不写评测文件」，
       而 rewrite 兜底时 `degraded` 恒为 None —— 而这一路正是「消解对照」
       主腿多路查询的来源，它降级了指标就失真，恰恰是护栏要拦的场景。
    """
    async def broken(messages, **kw):
        raise llm.LLMError("JSON 解析失败")
    monkeypatch.setattr(llm, "complete_json", broken)

    out = await RW.rewrite_node(_state())
    assert out["retrieval_queries"][0]["source"] == "verbatim"
    assert out["trace"][0]["degraded"], "兜底了却没留痕 —— 评测护栏拦不住"


async def test_normal_path_has_no_degradation(monkeypatch):
    """反向锁：正常三路扩展不该标降级。"""
    async def fake(messages, **kw):
        return [{"source": "verbatim", "text": "缓考需要什么条件？"},
                {"source": "keywords", "text": "缓考 条件"}]
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RW.rewrite_node(_state())
    assert out["trace"][0]["degraded"] is None
