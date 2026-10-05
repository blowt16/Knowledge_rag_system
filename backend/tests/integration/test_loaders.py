"""加载层在**真实语料**上的验证（附录 B + E.3 / E.4 / E.8）。

对应施工计划 M0-7 的测试点：
  ① 每份都出 chunk
  ② `current_chapter` 非空（B.2.1 / E.3.2 修复的证据）
  ③ `char_start/char_end` 递增，且规范化文本切片**逐字等于** chunk 正文
  ④ 06/07/08/09/10 五份首页**成文日期没被毁掉**（竖排反转早于清洗的证据）

另加一条语料画像断言：**OCR 页数应为 0**（§E.1 实测本语料扫描页为 0）。
曾经因为把「整份级」的字符数阈值当页级用，5 份公文各多送 1 页给 MinerU ——
本测试锁死这个回归。
"""

from __future__ import annotations

import pytest

from app.core.config import repo_path
from app.ingestion.chunker import chunk_text
from app.ingestion.clean import build_normalized_text
from app.ingestion.loaders import load
from app.ingestion.loaders.pdf import extract_chapter_marks

CORPUS = repo_path("corpus", "guet")
PDFS = sorted(CORPUS.glob("*.pdf")) if CORPUS.exists() else []

pytestmark = pytest.mark.skipif(not PDFS, reason="语料目录不存在")


def _pipeline(path):
    result = load(path)
    normalized, slices = build_normalized_text(
        [(p.page, p.text) for p in result.pages]
    )
    chunks = chunk_text(normalized)
    return result, normalized, slices, chunks


@pytest.fixture(scope="module")
def loaded():
    return {p.name: _pipeline(p) for p in PDFS}


# ---- ① 每份都出内容 ---------------------------------------------------

def test_all_documents_produce_chunks(loaded):
    assert len(loaded) == 10
    for name, (_r, _n, _s, chunks) in loaded.items():
        assert chunks, f"{name} 没有产出任何 chunk"


def test_no_missing_pages_in_corpus(loaded):
    """语料是完整公文，不应当有缺失页；有的话说明加载层在丢内容。"""
    for name, (result, *_rest) in loaded.items():
        assert result.missing_pages == [], f"{name} 出现缺失页 {result.missing_pages}"


def test_no_ocr_triggered_on_text_layer_corpus(loaded):
    """★ 回归锁：本语料 95 页全是文字层完好的 PDF，扫描页为 0（§E.1）。

    OCR 一旦被触发，说明质量闸门误判 —— 那会把完好的文字页送去 MinerU 白烧钱，
    而且**不影响正确性、极难发现**。
    """
    for name, (result, *_rest) in loaded.items():
        assert result.ocr_pages == [], (
            f"{name} 误触发 OCR 的页：{result.ocr_pages} —— 检查页级判据是否混用了整份级阈值"
        )


# ---- ② 章节（B.2.1 / E.3.2 的修复证据）-------------------------------

def test_chapters_extracted_for_most_documents(loaded):
    """PDF 的 current_chapter 原先**从未被写入**（E.3.2，实测 1244/1244 全为空串）。
    正则修复后应当有值。

    §E.4.2 实测：9 份有「章」，只有第 10 份真的没有 —— 不为个别文档补特例规则。
    """
    for name, (_r, normalized, _s, _c) in loaded.items():
        marks = extract_chapter_marks(normalized)
        if name.startswith("10_"):
            assert marks == [], "第 10 份本就没有「章」，不应有标记"
        else:
            assert marks, f"{name} 未抽到任何章节标记"


def test_chapter_marks_are_ordered_and_in_range(loaded):
    for name, (_r, normalized, _s, _c) in loaded.items():
        marks = extract_chapter_marks(normalized)
        offsets = [m.char_offset for m in marks]
        assert offsets == sorted(offsets), f"{name} 章节偏移未按序"
        assert all(0 <= o < len(normalized) for o in offsets), f"{name} 章节偏移越界"


# ---- ③ 偏移是参照系的硬约束 ------------------------------------------

def test_chunk_offsets_are_strictly_increasing(loaded):
    for name, (_r, _n, _s, chunks) in loaded.items():
        starts = [c.char_start for c in chunks]
        assert starts == sorted(starts), f"{name} chunk 起点未递增"
        assert len(set(starts)) == len(starts), f"{name} chunk 起点重复"
        for c in chunks:
            assert c.char_end > c.char_start, f"{name} chunk {c.chunk_index} 区间为空"


def test_chunk_offsets_slice_back_to_chunk_text(loaded):
    """★ char_start/char_end 是 `jump_target` 与引用回跳的定位依据。

    切片必须**逐字等于** chunk 正文 —— 对不上意味着偏移参照系算错了，
    引用会静默跳错位置。
    """
    for name, (_r, normalized, _s, chunks) in loaded.items():
        for c in chunks:
            sliced = normalized[c.char_start:c.char_end].strip()
            assert sliced == c.text, (
                f"{name} chunk {c.chunk_index} 偏移切片与正文不符\n"
                f"  切片: {sliced[:60]!r}\n  正文: {c.text[:60]!r}"
            )


def test_chunk_size_respected(loaded):
    from app.core.config import cfg

    limit = int(cfg("chunking.chunk_size", 500))
    for name, (_r, _n, _s, chunks) in loaded.items():
        for c in chunks:
            # 允许断句点带来的少量超出；但不应成倍超
            assert len(c.text) <= limit * 2, f"{name} chunk {c.chunk_index} 过长"


# ---- ④ 竖排恢复（E.3.3）----------------------------------------------

VERTICAL_DOCS = {
    # 文档 06 恰好卡在 50% —— 判据写成 >= 0.5 才能命中它
    "06_本科生管理规定_桂电学2019-24号.pdf": "2019年8月9日",
    "07_学生申诉处理办法_2025修订_桂电学2025-7号.pdf": "2025年7月10日",
    "08_本科生产教融合实习学生管理规定.pdf": "2026年4月8日",
    "10_参军入伍和退役复学学生优待政策_桂电2021-2号.pdf": "2021年1月11日",
}


def test_vertical_cover_pages_recovered_with_dates(loaded):
    """★ 竖排恢复的核心断言：**成文日期必须还在**。

    文档自己警告：「若先清洗再反转，成文日期会被毁掉」——
    因为 `9` / `8` / `2019` 这类独立数字行会被 `^\\d{1,4}$` 当页码删掉。

    2026-10-05 实测发现：光「反转行序」不够，**必须同时合并各行** ——
    只反转不合并的话，那些数字仍是独立行，照样被清洗吃掉。
    """
    for name, expected_date in VERTICAL_DOCS.items():
        if name not in loaded:
            continue
        _r, normalized, _s, _c = loaded[name]
        head = normalized[:300]
        assert expected_date in head, (
            f"{name} 首页成文日期 {expected_date} 丢失 —— 竖排恢复或被清洗破坏了\n"
            f"  实际首页: {head[:120]!r}"
        )


def test_vertical_cover_text_is_readable_order(loaded):
    """恢复后的封面应是「各单位、各部门：现将《…》印发给你们」的正常语序。"""
    for name in VERTICAL_DOCS:
        if name not in loaded:
            continue
        _r, normalized, _s, _c = loaded[name]
        head = normalized[:200]
        assert "各单位、各部门" in head
        # 倒序时「门部各」会出现
        assert "门部各、" not in head, f"{name} 首页仍是倒序"


def test_horizontal_documents_keep_doc_number(loaded):
    """横向文档（01–05）不受竖排逻辑影响，文号应完好 —— 这正是 BM25 的用武之地。"""
    expected = {
        "01_本科生专业分流及转专业管理办法_桂电教2021-6号.pdf": "桂电教〔2021〕6",
        "03_本科生学业预警及学业退学实施细则_桂电教2025-28号.pdf": "桂电教〔2025〕28",
        "04_普通高等教育本科学生学籍管理规定_2025修订_桂电教2025-27号.pdf": "桂电教〔2025〕27",
    }
    for name, doc_number in expected.items():
        if name not in loaded:
            continue
        _r, normalized, _s, _c = loaded[name]
        assert doc_number in normalized[:200], f"{name} 文号 {doc_number} 丢失"
