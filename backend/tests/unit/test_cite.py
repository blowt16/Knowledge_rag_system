"""节点 10 cite —— 引用计算与声明级校验（§8.2 M3-4 的测试点）。

代码 M0 就写好了，本轮补护栏、不改行为。三条最容易写错的：

1. **越界判定的上界是 `len(evidence)`，不是 `len(candidates)`**。
   `candidates` 是 RRF 融合后的全量（可达 40 条），拿它做上界会让
   「只有 5 条证据却写 `[7]`」这类**真越界漏检** —— 而越界标记是幻觉的直接证据。
2. **偏移相对「渲染前的原始答案文本」，单位 Unicode 码点**。
   前端靠这两个偏移把对应句子置灰；差一个字符就标错位置，且肉眼很难发现。
   所以每条都断言 `answer[char_start:char_end] == sentence`（这就是前端的校验 A）。
3. **不拦截，只标注**。token 一旦流出就已到达用户，校验无法撤回内容 ——
   越界标记只记录，不改写答案、不抛异常。
"""

from __future__ import annotations

import pytest

from app.graph.nodes.cite import build_citations, build_verify_report, cite_node
from app.graph.nodes.generate import is_conclusion_sentence
from app.graph.state import Chunk, new_state


def _chunk(cid: str, *, doc: str = "d1", name: str = "文档一", page: int = 1,
           chapter: str = "", text: str = "", images: list[str] | None = None,
           bbox: list[dict] | None = None, escalated: bool = False,
           char_start: int = 0, char_end: int = 0) -> Chunk:
    return Chunk(
        chunk_id=cid, text=text, document_id=doc, document_name=name, page=page,
        chapter=chapter, images=images or [], bbox=bbox or [],
        escalated=escalated, char_start=char_start, char_end=char_end,
    )


def _evidence(n: int) -> list[Chunk]:
    return [_chunk(f"c{i}", doc=f"d{i}", name=f"文档{i}") for i in range(1, n + 1)]


# ============================================================
# 越界标记
# ============================================================

def test_out_of_range_marker_is_flagged_with_locatable_offsets():
    """5 条证据却写 `[7]` → 必须进 `invalid_markers`，且偏移能切出原标记。"""
    answer = "结论甲[1]。结论乙[7]。"
    report = build_verify_report(answer, _evidence(5))

    assert [m.marker for m in report.invalid_markers] == [7]
    bad = report.invalid_markers[0]
    assert answer[bad.char_start:bad.char_end] == "[7]"


def test_upper_bound_is_evidence_count_not_candidate_count():
    """上界是**本次证据条数**：第 5 条合法、第 6 条越界。"""
    answer = "甲[5]。乙[6]。"
    report = build_verify_report(answer, _evidence(5))
    assert [m.marker for m in report.invalid_markers] == [6]


def test_out_of_range_marker_never_blocks():
    """不拦截、只标注：越界标记不抛异常，也不产出引用条目。"""
    answer = "甲[9]。"
    assert build_citations(answer, _evidence(2)) == []
    assert build_verify_report(answer, _evidence(2)).invalid_markers


# ============================================================
# 无依据句
# ============================================================

def test_uncited_conclusion_sentence_has_sliceable_offsets():
    """无标记的结论句进 `uncited_claims`，偏移能原样切出那句话。"""
    answer = "转专业需要满足条件一[1]。申请材料需提前一周提交。"
    report = build_verify_report(answer, _evidence(1))

    assert [c.sentence for c in report.uncited_claims] == ["申请材料需提前一周提交。"]
    claim = report.uncited_claims[0]
    assert answer[claim.char_start:claim.char_end] == claim.sentence
    assert (report.total_claims, report.cited_claims) == (2, 1)


def test_sentence_offset_skips_leading_whitespace():
    """句子的偏移要**跳过前导空白** —— 否则置灰会把上一句的换行也标进去。"""
    answer = "甲[1]。\n 申请材料需要提前提交。"
    report = build_verify_report(answer, _evidence(1))

    claim = report.uncited_claims[0]
    assert answer[claim.char_start:claim.char_end] == claim.sentence
    assert not claim.sentence.startswith("\n")


def test_offsets_are_code_points_so_emoji_do_not_shift_them():
    """偏移按 **Unicode 码点**算（星平面字符占 1 个码点、2 个 UTF-16 单元）。

    后端自己就会把 📚 写进答案，所以这条不是理论问题；前端要做
    `toCp` 转换，判据就是「切片必须逐字等于 sentence」。
    """
    answer = "📚资料显示甲[1]。申请材料需要提前提交。"
    report = build_verify_report(answer, _evidence(1))

    claim = report.uncited_claims[0]
    assert answer[claim.char_start:claim.char_end] == claim.sentence


def test_only_valid_markers_count_as_supported():
    """只有**合法**标记才算「有依据」：写了 `[7]` 的句子仍按无依据记。"""
    answer = "只有越界标记的句子[7]。"
    report = build_verify_report(answer, _evidence(2))

    assert report.cited_claims == 0
    assert [c.sentence for c in report.uncited_claims] == ["只有越界标记的句子[7]。"]


@pytest.mark.parametrize("sentence,expected", [
    ("转专业需要满足条件一[1]。", True),
    ("缓考怎么申请？", False),          # 疑问句
    ("请提前一周提交材料。", False),     # 祈使 / 建议句
    ("建议咨询教务处。", False),
    ("综上。", False),                   # 纯过渡句
    ("太短。", False),                   # 过短
    ("", False),
    # ★ 元陈述（§3.5.3 节点 9 的排除项）：断言的不是文档中的事实，而是
    #   「我查没查、资料里有没有」。M3 的 K-1：实测「资料中未找到针对黄色、
    #   橙色、红色预警各自具体后果的进一步规定。」被当成结论句**标了灰**。
    ("资料中未找到针对黄色预警各自具体后果的进一步规定。", False),
    ("知识库中未找到相关规定。", False),
    ("未在文档中提及该情形。", False),
    ("我查阅了相关资料。", False),        # 方案 §3.5.3 给的原例
    ("本文档未包含此类条款。", False),
])
def test_conclusion_sentence_judgement_is_conservative(sentence, expected):
    """「结论句」判定必须保守：拿不准的一律不算，**宁可漏，不可错**。"""
    assert is_conclusion_sentence(sentence) is expected


def test_uncertain_sentences_never_enter_uncited_claims():
    """保守判定的后果直接体现在报告里：疑问句/建议句不进无依据清单。"""
    answer = "缓考怎么申请？[1]请提前一周提交材料。"
    report = build_verify_report(answer, _evidence(1))
    assert report.uncited_claims == []


# ============================================================
# 引用条目本身
# ============================================================

def test_only_referenced_chunks_become_citations():
    """只输出**答案中实际引用**的 chunk，不是全部检索结果。"""
    citations = build_citations("甲[2]。", _evidence(3))
    assert [c.marker for c in citations] == [2]
    assert citations[0].chunk_id == "c2"


def test_repeated_marker_yields_one_citation():
    citations = build_citations("甲[1]。乙[1]。", _evidence(2))
    assert [c.marker for c in citations] == [1]


def test_citation_carries_full_traceability():
    """溯源信息一个都不能少 —— 少一个前端就跳不到位。"""
    chunk = _chunk(
        "c1", doc="d9", name="文档九", page=3, chapter="第二章 学籍",
        text="正文" * 200, images=["p3_0.jpeg"], escalated=True,
        char_start=100, char_end=140,
        bbox=[{"page": 3, "x0": 1.5, "x1": 2.5, "top": 3.5, "bottom": 4.5}],
    )

    citation = build_citations("结论[1]。", [chunk])[0]

    assert citation.document_name == "文档九"
    assert citation.chapter == "第二章 学籍"
    assert citation.page == 3
    assert citation.snippet == ("正文" * 200)[:200]
    assert citation.chunk_id == "c1"
    assert citation.escalated is True                       # 越权引用要能标出来
    assert citation.images == ["p3_0.jpeg"]                 # ★ 文件名，不是签名 URL
    assert citation.jump_target.document_id == "d9"         # ★ 调 /file 需要主键
    assert citation.jump_target.page == 3
    assert (citation.jump_target.char_start, citation.jump_target.char_end) == (100, 140)
    assert citation.jump_target.boxes[0].x0 == 1.5


def test_malformed_bbox_entries_are_skipped_not_fatal():
    """bbox 是 Chroma 里取出来的 JSON，脏一条不该让整条引用炸掉。"""
    chunk = _chunk("c1", bbox=[
        {"page": 1, "x0": "坏数据", "x1": 2, "top": 3, "bottom": 4},
        {"page": 1, "x0": 1, "x1": 2, "top": 3, "bottom": 4},
    ])
    citation = build_citations("甲[1]。", [chunk])[0]
    assert len(citation.jump_target.boxes) == 1


# ============================================================
# 节点级
# ============================================================

async def test_cite_node_skips_everything_on_refusal():
    """拒答路径不发 citations / verify（两个字段都保持空）。"""
    state = new_state(decision="REFUSED_NO_EVIDENCE", answer="依据不足。",
                      evidence=_evidence(2))
    out = await cite_node(state)

    assert out["citations"] == []
    assert out["verify_report"].total_claims == 0
    assert out["trace"][0]["node"] == "cite"


async def test_cite_node_produces_both_outputs_in_one_pass():
    state = new_state(decision="ANSWERED",
                      answer="甲[1]。申请材料需要提前提交。", evidence=_evidence(1))
    out = await cite_node(state)

    assert [c.marker for c in out["citations"]] == [1]
    assert [c.sentence for c in out["verify_report"].uncited_claims] == ["申请材料需要提前提交。"]
