"""切分器的原子区间（§3.4.1 表格不被切断）。"""
from __future__ import annotations

from app.ingestion.chunker import chunk_text


def test_atomic_range_is_never_split():
    """★原子区间内部不切 —— 哪怕里面有断句标点、且区间超过 chunk_size。"""
    body = "甲；乙；丙；丁；戊；己；庚；辛；壬；癸；" * 8     # 大量；且远超 60 字
    text = "前文。\n\n" + body + "\n\n后文。"
    start = text.index(body)
    spans = chunk_text(text, chunk_size=60, overlap=10, min_size=5,
                       atomic=[(start, start + len(body))])
    holding = [s for s in spans if s.char_start <= start < s.char_end]
    assert len(holding) == 1, "原子区间必须完整落在同一个 chunk 里"
    assert body in holding[0].text


def test_atomic_range_is_not_merged_with_prose():
    """★原子区间不与相邻正文合并 —— 表格块要独立成块（Unstructured 的硬保证）。"""
    body = "甲；乙；丙"
    text = "前文很长很长很长很长很长很长很长很长。" + body + "后文很长很长很长很长很长很长很长。"
    start = text.index(body)
    spans = chunk_text(text, chunk_size=500, overlap=50, min_size=5,
                       atomic=[(start, start + len(body))])
    holder = [s for s in spans if s.char_start <= start < s.char_end][0]
    assert "前文" not in holder.text and "后文" not in holder.text


def test_atomic_overlapping_page_boundary_is_not_cut():
    """★表格组跨页边界时，原子优先 —— 「表格不切断」高于「chunk 不跨页」。"""
    body = "甲；乙；丙；丁"
    text = "第一页正文。" + body + "第二页正文。"
    b = text.index(body)
    page_boundary = b + 3                    # 边界落在组内部
    spans = chunk_text(text, chunk_size=500, overlap=50, min_size=5,
                       boundaries=[page_boundary], atomic=[(b, b + len(body))])
    holder = [s for s in spans if s.char_start <= b < s.char_end][0]
    assert body in holder.text


def test_atomic_none_behaves_as_before():
    """不传 atomic 时行为与改造前一致 —— 回归保护。"""
    text = "第一句。第二句。第三句。"
    assert len(chunk_text(text, chunk_size=10, overlap=2, min_size=2)) == len(
        chunk_text(text, chunk_size=10, overlap=2, min_size=2, atomic=None))


def test_atomic_ranges_are_sorted_and_clamped():
    """乱序/越界的区间不炸 —— 调用方传错不该让整篇入库失败。"""
    text = "甲乙丙丁戊己庚辛。"
    spans = chunk_text(text, chunk_size=4, overlap=1, min_size=2,
                       atomic=[(99, 200), (2, 4), (0, 1)])
    assert "".join(s.text for s in spans).startswith("甲")
