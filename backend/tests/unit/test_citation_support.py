"""引用支持度字面校验（§4.2）。

事故形状：一句话来自《06》，却被挂到《02》的编号上。
这里的测试不碰 LLM，只验规则本身：支持句不误标、错编号角标提示、
全部不支持才整句置灰、偏移能原样切出句子。
"""

from __future__ import annotations

from app.graph.nodes.citation_support import (
    check_citation_support, content_units, normalize,
)
from app.graph.state import Chunk


def _chunk(cid: str, text: str, *, name: str = "文档") -> Chunk:
    return Chunk(chunk_id=cid, text=text, document_id=cid, document_name=name)


ACCIDENT = "专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。"
DOC06 = "第三十五条 专科起点本科的毕（结）业证书中注明“专科起点本科学习”。"
DOC02 = "第二章 学位授予条件。本科毕业生符合条件者授予学士学位。"


def test_accident_sentence_is_unsupported_by_wrong_document():
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", DOC02)])
    assert [c.sentence for c in claims] == [ACCIDENT]
    assert claims[0].unsupported_markers == [1]
    assert [m.marker for m in markers] == [1]
    assert supported == 0


def test_same_sentence_is_supported_by_its_own_document():
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", DOC06)])
    assert claims == [] and markers == [] and supported == 1


def test_pdf_line_wrap_is_normalized():
    """原文“专\\n科起点本科学习”必须和一行写法视为同一句。"""
    wrapped = "第三十五条 专科起点本科的毕（结）业证书中注明“专\n科起点本科学习”。"
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", wrapped)])
    assert claims == [] and markers == [] and supported == 1


def test_number_mismatch_is_reported():
    answer = "免修成绩按 90 分记录[1]。"
    got, markers, _ = check_citation_support(
        answer, [_chunk("c1", "免修成绩按 85 分记录。")])
    assert got[0].reason == "number_mismatch"
    assert markers[0].reason == "number_mismatch"


def test_short_sentence_is_not_judged():
    """内容单元少于 3 个 → 不判，避免误标。"""
    answer = "AB 12[1]。"
    assert len(content_units(answer)) < 3
    claims, markers, supported = check_citation_support(answer, [_chunk("c1", DOC02)])
    assert claims == [] and markers == [] and supported == 0


def test_partial_support_flags_only_the_bad_marker():
    answer = "转专业需要提交申请表[1][2]。"
    got, markers, supported = check_citation_support(answer, [
        _chunk("c1", "转专业需要提交申请表。"),
        _chunk("c2", DOC02),
    ])
    assert got == []
    assert supported == 1
    assert [m.marker for m in markers] == [2]


def test_all_unsupported_marks_the_sentence():
    answer = "转专业需要提交申请表[1][2]。"
    got, markers, supported = check_citation_support(answer, [
        _chunk("c1", DOC02), _chunk("c2", DOC06),
    ])
    assert [c.sentence for c in got] == [answer]
    assert got[0].unsupported_markers == [1, 2]
    assert [m.marker for m in markers] == [1, 2]
    assert supported == 0


def test_offsets_slice_the_original_sentence():
    answer = "\n 转专业需要提交申请表[1]。"
    got, _, _ = check_citation_support(answer, [_chunk("c1", DOC02)])
    claim = got[0]
    assert answer[claim.char_start:claim.char_end] == claim.sentence


def test_units_and_normalization():
    assert normalize("ＡＢＣ，１２3\n") == "ABC123"
    units = content_units("转专业申请2025年")
    assert "转专" in units and "2025" in units
