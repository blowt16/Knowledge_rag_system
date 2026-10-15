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
    """**别写死份数** —— 语料会增长（10 → 15 只用了几天），
    写死会在加文档时误报「加载层坏了」，而真正要守的是「有几份就出几份」。"""
    assert PDFS, "语料目录里一份 PDF 都没有"
    assert len(loaded) == len(PDFS)
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

    ⚠️ **2026-10-06 更新（M3 的 K-2 跟进）**：这里原本断言第 10 份
       「本就没有『章』，不应有标记」。实测把话说完整是：它**确实没有「第X章」**，
       但它用「一、二、」作顶层条号 —— 整份 5 条 chunk 因此一条章节都抽不出来，
       引用只能落到「只跳页」（§9.9 K-2）。

       现在加的是**有门槛的回退**（只在完全抽不到章标记时才认「一、」），
       不是按文件名开的特例 —— 另外 9 份有「第X章」→ 不启用回退，一行不受影响
       （回归锁见 `tests/unit/test_chapter_marks.py`）。
    """
    for name, (_r, normalized, _s, _c) in loaded.items():
        marks = extract_chapter_marks(normalized)
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


# ---- ⑤ 表格结构化（Task 5）--------------------------------------------

def test_pdf_table_is_serialized_with_column_names():
    """★表格在 PageText.text 里是键值行，不再是拍平的单格行。"""
    from app.ingestion.loaders.pdf import load_pdf
    from app.ingestion.tables import GROUP_MARK

    pdf = CORPUS / "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf"
    if not pdf.exists():
        pytest.skip("语料不在仓库中")
    res = load_pdf(pdf)
    merged = "\n".join(p.text for p in res.pages)
    assert GROUP_MARK in merged, "表头行必须出现"
    # ⚠️ 别断言 `课程内容：无偿献血` —— doc 12 的「无偿献血」落在 p11，
    #    那是 7 列的表、继承的是 6 列表头，按 Task 3 的裁定走**位置标注**，
    #    列名形态根本不存在（第一版断言就是照 p8 的形态写的，是错的）。
    #    真正要守的是：值还在、且被某个标签正确带着。
    assert "无偿献血" in merged, "数据行必须出现"
    # 列名形态（列数相符的表，如 p8）
    assert "课程内容：完成相应培训课时" in merged, "列数相符的表应按列名配对"
    # 位置标注形态（列数不符的表，如 p11）
    assert "列3：无偿献血" in merged, "列数不符的表应按位置标注，值不得错挂列名"


def test_pdf_table_text_appears_only_once():
    """★表格内容不能出现两遍（拍平版 + 序列化版）。"""
    from app.ingestion.loaders.pdf import load_pdf

    pdf = CORPUS / "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf"
    if not pdf.exists():
        pytest.skip("语料不在仓库中")
    res = load_pdf(pdf)
    merged = "\n".join(p.text for p in res.pages)
    assert merged.count("捐献造血干细胞") == 1, "同一个单元格出现了多次"


def test_multi_table_page_keeps_reading_order():
    """★多表页的正文不能被挤到页尾。

    踩过的坑：把「正文块」按前缀排干时，**假设了 `get_text("blocks")` 按 y 递增**。
    实测 doc 11 的 p3/p4/p6 块表**第 0 项是页脚页码块**（top=776.4，比页上最高的
    表顶 541.5 还大），排干在 cursor=0 就卡死、一个块都插不进去 —— 整页变成
    「以【表头】开头」，表格上方的引导正文（「（一）科研成果类…」）全被推到页尾。
    内容没丢，但阅读顺序错乱，而且**是静默的**。
    """
    from app.ingestion.loaders.pdf import load_pdf
    from app.ingestion.tables import GROUP_MARK

    pdf = CORPUS / "11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf"
    if not pdf.exists():
        pytest.skip("语料不在仓库中")
    res = load_pdf(pdf)
    for n in (3, 4, 6):              # 实测这三页各含两张表
        text = next(p.text for p in res.pages if p.page == n)
        head = text[:text.find(GROUP_MARK)]
        assert text.find(GROUP_MARK) > 0, f"p{n} 以表格组开头"
        assert head.strip(), f"p{n} 第一个表格组之前没有任何正文 —— 阅读顺序错乱"


def test_every_table_group_is_blank_line_delimited():
    """★跨任务接口契约：Task 6 靠「`【表头】` 到下一个空行」切段。

    组**前**的空行是构造保证（每组必先 `append("")`）；组**后**的那一行
    第一版不是构造保证 —— `append("")` 发生在正文块之前，两张表之间的正文块
    会挤掉它，于是该组只跟后面隔一个 `\\n`。当前语料恰好不触发（触发要求
    块表按 y 有序，而多表页的块表第 0 项是页脚），但契约不能靠巧合。

    判据：按空行切页文本，凡是**含**组标记却不**以**组标记开头的块，
    就说明有一组没被空行定界。
    """
    from app.ingestion.loaders.pdf import load_pdf
    from app.ingestion.tables import GROUP_MARK

    for name in ("11_创新创业拓展课程学分认定管理办法_桂电教2026-13号",
                 "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号"):
        pdf = CORPUS / f"{name}.pdf"
        if not pdf.exists():
            continue
        res = load_pdf(pdf)
        for pg in res.pages:
            for block in pg.text.split("\n\n"):
                if block.startswith(GROUP_MARK):
                    continue
                assert GROUP_MARK not in block, (
                    f"{name} p{pg.page} 有一组没被空行定界：{block[:60]!r}")


def test_table_group_marker_survives_cleaning():
    """★表头行不能被「跨页重复行」规则当成页眉删掉。

    实测：表头行 35 字 ≤ `find_repeated_lines` 的 `max_len=40`，
    而它每页都重复。8 页文档的阈值是 `int(8*0.6)=4`，
    表头在 4 页出现 → **恰好达到阈值会被整片删除**（doc 11 正是 8 页）。
    结果就是改造对那份文档完全失效，且不报任何错。
    """
    from app.ingestion.clean import build_normalized_text
    from app.ingestion.tables import GROUP_MARK

    hdr = GROUP_MARK + "课程类别｜课程项目｜课程内容｜参考学分｜开课单位｜获得学分方式"
    pages = [(i + 1, f"第{i + 1}页正文。\n{hdr}\n课程类别：甲｜参考学分：5分") for i in range(8)]
    normalized, _ = build_normalized_text(pages)
    assert normalized.count(GROUP_MARK) == 8, "每个表头行都必须留下"
