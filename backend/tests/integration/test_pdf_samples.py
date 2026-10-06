"""回归样本 PDF：扫描件 与 子集字体（§E.8.1 / R3）。

**这两份样本存在的理由**：现有 10 份语料全是文字层完好的 PDF，加载层的
第二路（OCR）分支与质量闸门**从来没被真实文件跑过**。没有样本 = 没有证据。

样本由 `backend/tools/make_regression_pdfs.py` 生成、进版本库（小、可复现）。

⚠️ **最后一条用例才是重点**：语料里 10 份文档**一页都不许被判不合格**。
   判错方向的代价不对称 —— 漏判只是某个乱码文件答得差；**误判会让整份
   公文走云端 OCR**（花钱、慢、还可能失败），而 10 份语料是基线。
"""

from __future__ import annotations

import pytest
import fitz

from app.core.config import repo_path
from app.ingestion.loaders import pdf as pdf_loader
from app.ingestion.loaders.pdf import _page_is_usable, document_looks_scanned

SAMPLES = repo_path("backend", "tests", "fixtures", "pdf_samples")
SCANNED = SAMPLES / "scanned_sample.pdf"
SUBSET = SAMPLES / "subset_font_sample.pdf"

CORPUS = repo_path("corpus", "guet")
CORPUS_PDFS = sorted(CORPUS.glob("*.pdf")) if CORPUS.exists() else []

pytestmark = pytest.mark.skipif(
    not SCANNED.exists() or not SUBSET.exists(),
    reason="样本未生成：cd backend && uv run python tools/make_regression_pdfs.py",
)


# ---- 扫描件样本 -------------------------------------------------------

def test_scanned_sample_really_has_no_text_layer():
    """先确认样本本身是对的 —— 否则后面的断言证明不了任何事。"""
    doc = fitz.open(SCANNED)
    try:
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        assert text.strip() == "", f"扫描件样本不该有文字层，却抽出了 {len(text)} 字"
    finally:
        doc.close()


def test_scanned_sample_is_judged_scanned():
    doc = fitz.open(SCANNED)
    try:
        texts = [doc[i].get_text() for i in range(doc.page_count)]
        assert document_looks_scanned(doc, texts) is True
    finally:
        doc.close()


def test_scanned_sample_routes_every_page_to_ocr(monkeypatch):
    """整份级初判命中 → **所有页**都送 OCR（而不是逐页判）。"""
    seen: list[int] = []

    def _fake_ocr(pdf_path, pages, image_dir):
        seen.extend(pages)
        return []

    monkeypatch.setattr(pdf_loader, "_ocr_pages", _fake_ocr)
    result = pdf_loader.load_pdf(SCANNED)

    assert seen == [1, 2], f"应整份 2 页都送 OCR，实际 {seen}"
    assert result.ocr_pages == [1, 2]
    assert result.source == "mineru"


# ---- 子集字体样本 -----------------------------------------------------

def _subset_stats() -> dict:
    doc = fitz.open(SUBSET)
    try:
        page = doc[0]
        text = page.get_text()
        chars = [c for c in text if not c.isspace()]
        cjk = sum(1 for c in chars if "一" <= c <= "鿿")
        punct = sum(1 for c in chars if c.isascii() and not c.isalnum())
        return {
            "fonts": [f[3] for f in page.get_fonts()],
            "cjk_ratio": cjk / max(len(chars), 1),
            "ascii_punct_ratio": punct / max(len(chars), 1),
            "text_len": len(text.strip()),
            "page": page,
            "doc": doc,
        }
    finally:
        pass


def test_subset_font_sample_shape():
    """样本要真落在三个判据的窗口里（子集字体 + 几乎无 CJK + 标点占多数）。"""
    s = _subset_stats()
    try:
        assert all("+" in f for f in s["fonts"]), f"字体没带子集前缀: {s['fonts']}"
        assert s["cjk_ratio"] < 0.05, s["cjk_ratio"]
        assert s["ascii_punct_ratio"] > 0.4, s["ascii_punct_ratio"]
        assert s["text_len"] > 0, "样本不能是空文字层（否则测的是『空页』那条判据）"
    finally:
        s["doc"].close()


def test_subset_font_page_is_rejected_by_quality_gate():
    """★ 子集字体页必须被判为「文字层不可信」→ 该页单独走 OCR（§E.8.1）。"""
    s = _subset_stats()
    try:
        page, text = s["page"], s["page"].get_text()
        assert _page_is_usable(page, text) is False, (
            "子集字体把汉字映射成 ASCII 的页被判成『可用』—— "
            "它会以标点汤的形式直接进正文，而正文是检索与生成的输入"
        )
    finally:
        s["doc"].close()


def test_subset_font_sample_routes_to_ocr(monkeypatch):
    seen: list[int] = []

    def _fake_ocr(pdf_path, pages, image_dir):
        seen.extend(pages)
        return []

    monkeypatch.setattr(pdf_loader, "_ocr_pages", _fake_ocr)
    pdf_loader.load_pdf(SUBSET)

    assert seen == [1, 2], f"两页都应走 OCR，实际 {seen}"


# ---- 反向控制：语料一页都不许被误判 -----------------------------------

@pytest.mark.skipif(not CORPUS_PDFS, reason="语料目录不存在")
@pytest.mark.parametrize("path", CORPUS_PDFS, ids=lambda p: p.name[:24])
def test_corpus_pages_are_all_usable(path):
    """★ 假阳性的护栏：10 份语料的每一页都必须判为可用。

    这条红了说明新加的质量判据打到了真实公文 —— 代价是整份走云端 OCR，
    而语料是全部基线数字的来源。
    """
    doc = fitz.open(path)
    try:
        bad = [i + 1 for i in range(doc.page_count)
               if not _page_is_usable(doc[i], doc[i].get_text())]
    finally:
        doc.close()
    assert bad == [], f"{path.name} 第 {bad} 页被误判为不可用（应为空列表）"
