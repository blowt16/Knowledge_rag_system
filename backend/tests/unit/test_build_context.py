"""节点 8 build_context —— 上下文组装（§8.2 M3-2 的测试点）。

代码在 M0 就写好了（`build_evidence`），**本文件不改行为**，只把「原方案只写
『裁剪到 token 预算』六个字、四项规格全靠实现者自己定」的那四项钉住 ——
钉不住的话，将来任何一次「顺手优化」都会静默改掉**编号与证据的对应关系**，
而引用错位比漏引用更糟。

四项规格：
- 单位：**整块丢弃**，不做块内截断（截断会让 `char_start/bbox` 指向残缺文本，
  引用回跳会跳错）
- 预算：检索上下文 8,000 token（防御性上限，Top-5 通常远达不到）
- 「低分」按 **rerank 分**（`reranked` 的原始顺序）；分组只影响展示顺序
- **先裁后编号**：裁掉中间某条会让编号出现空洞（有 `[1]` `[3]` 没有 `[2]`），
  而模型仍会照抄编号
"""

from __future__ import annotations

import pytest

from app.graph.nodes import build_context as BC
from app.graph.state import Chunk

# 测试里把预算压到 300「字」、并把 token 计数换成字数 —— 让边界可精确断言
BUDGET = 300
HEADER_OVERHEAD = 40          # build_evidence 给头部信息留的固定开销


@pytest.fixture(autouse=True)
def _cheap_budget(monkeypatch):
    monkeypatch.setattr(BC, "count_tokens", lambda text: len(text))
    monkeypatch.setattr(
        BC, "cfg",
        lambda key, default=None: BUDGET
        if key == "context.retrieval_context_budget" else default,
    )


def _chunk(cid: str, text: str, *, doc: str, name: str, idx: int = 0,
           page: int = 1, chapter: str = "", images: list[str] | None = None) -> Chunk:
    return Chunk(
        chunk_id=cid, text=text, document_id=doc, document_name=name,
        chunk_index=idx, page=page, chapter=chapter, images=images or [],
    )


def _cost(text: str) -> int:
    return len(text) + HEADER_OVERHEAD


def test_over_budget_drops_whole_chunks():
    """超出预算 → 整块丢弃（按 rerank 顺序，从低分那端开始丢）。"""
    a = _chunk("d1:0", "甲" * 100, doc="d1", name="文档一", idx=0)
    b = _chunk("d2:0", "乙" * 100, doc="d2", name="文档二", idx=0)
    c = _chunk("d3:0", "丙" * 100, doc="d3", name="文档三", idx=0)
    assert _cost(a.text) + _cost(b.text) <= BUDGET < _cost(a.text) + _cost(b.text) + _cost(c.text)

    context, evidence = BC.build_evidence([a, b, c])

    assert [ch.chunk_id for ch in evidence] == ["d1:0", "d2:0"], "第三条该被整块丢掉"
    assert "丙" not in context
    assert "甲" in context and "乙" in context


def test_dropped_chunk_leaves_no_partial_text():
    """**不做块内截断** —— 被丢的 chunk 连一个字的碎片都不该留在 context 里。

    截断会让该 chunk 的 `char_start/char_end` 与 `bbox`（跳转定位依据）
    指向残缺文本，而 `evidence` 里仍然有它 —— 引用回跳会跳到半句话上。
    """
    small = _chunk("d1:0", "乙" * 100, doc="d1", name="文档一", idx=0)   # 精排分更高
    big = _chunk("d2:0", "甲" * 400, doc="d2", name="文档二", idx=0)     # 装不下，整块丢

    context, evidence = BC.build_evidence([small, big])

    assert [ch.chunk_id for ch in evidence] == ["d1:0"]
    assert "甲" not in context, "超预算的 chunk 必须整块丢，不能截一半"


def test_first_chunk_is_kept_even_if_over_budget():
    """一条都装不下时也要留第一条 —— 检索到的就是它，全丢等于没证据。

    （`kept and used + cost > budget` 里的 `kept and` 就是为这个。）
    """
    only = _chunk("d1:0", "甲" * 400, doc="d1", name="文档一", idx=0)
    context, evidence = BC.build_evidence([only])
    assert [ch.chunk_id for ch in evidence] == ["d1:0"]
    assert "甲" * 400 in context


def test_numbering_is_contiguous_after_drop():
    """编号 `[1]…[N]` 连续无空洞，且 N == len(evidence)。"""
    chunks = [
        _chunk(f"d{i}:0", chr(0x4E00 + i) * 30, doc=f"d{i}", name=f"文档{i}", idx=0)
        for i in range(5)
    ]
    assert _cost(chunks[0].text) * 4 <= BUDGET < _cost(chunks[0].text) * 5

    context, evidence = BC.build_evidence(chunks)

    assert len(evidence) == 4
    for n in range(1, 5):
        assert f"[{n}]" in context, f"缺编号 [{n}]"
    assert "[5]" not in context, "被裁掉的条目不该留下编号 —— 模型会照抄它"


def test_evidence_order_is_grouped_not_rerank_order():
    """★ `evidence` 的顺序 ≠ `reranked` 的顺序 —— 这就是「不能拿 reranked 做映射」的证据。

    拼装前要「按文档分组 → 组内按 chunk_index 排序」，所以 prompt 里
    `[1]` 很可能**不是**精排第一条。拿 `reranked` 去映射 `[n]`，
    `[1]` 会指向错的 chunk。
    """
    reranked = [
        _chunk("d2:1", "B2", doc="d2", name="文档二", idx=1),
        _chunk("d1:1", "A2", doc="d1", name="文档一", idx=1),
        _chunk("d2:0", "B1", doc="d2", name="文档二", idx=0),
        _chunk("d1:0", "A1", doc="d1", name="文档一", idx=0),
    ]

    context, evidence = BC.build_evidence(reranked)

    assert [ch.chunk_id for ch in evidence] == ["d2:0", "d2:1", "d1:0", "d1:1"]
    assert [ch.chunk_id for ch in evidence] != [ch.chunk_id for ch in reranked]
    # 编号跟着 evidence 走：[1] 是 B1，不是精排第一条 B2
    assert context.index("B1") < context.index("B2") < context.index("A1") < context.index("A2")
    assert "[1] 《文档二》" in context


def test_header_carries_locator_and_images():
    """头部要带文档名/章节/页码；图片只记占位（描述在 E.4.5 已删）。"""
    ch = _chunk("d1:0", "正文", doc="d1", name="文档一", idx=0,
                page=3, chapter="第二章 学籍", images=["p3_0.jpeg"])

    context, _ = BC.build_evidence([ch])

    assert "《文档一》" in context
    assert "第二章 学籍" in context
    assert "第 3 页" in context
    assert "p3_0.jpeg" in context


def test_empty_input_yields_empty_pair():
    assert BC.build_evidence([]) == ("", [])
