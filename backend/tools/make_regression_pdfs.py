"""造两份回归样本 PDF —— 扫描件 与 子集字体（汉字被映射成 ASCII 的形态）。

    cd backend && uv run python tools/make_regression_pdfs.py

产物落在 `backend/tests/fixtures/pdf_samples/`，**进版本库**（小、可复现、
由本脚本生成，不要手工改）。

为什么要有这两份（§E.8.1 / 风险登记册 R3）：
    现有 10 份语料全是"文字层完好"的 PDF —— **加载层的第二路（OCR）分支
    与质量闸门从来没被真实文件跑过**。没有样本就等于没有证据。

两份样本分别打的是闸门的哪一条：

| 样本 | 期望触发 | 判据 |
|---|---|---|
| `scanned_sample.pdf` | 整份级初判 | 平均每页有效字符 < 50（页面只有图、没有文字层） |
| `subset_font_sample.pdf` | 页级判据 | 子集字体占比 ≥0.3 **且** CJK <0.05 **且** ASCII 标点 >0.4 |

⚠️ 子集字体样本**刻意不放整页大图** —— 否则会同时触发「图片覆盖率 ≥0.8」，
    那这条用例就证明不了子集字体判据本身生效（两个原因混在一起）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "corpus" / "guet"
OUT = REPO / "backend" / "tests" / "fixtures" / "pdf_samples"

# 汉字被映射成 ASCII 时，抽出来的典型形态：几乎全是 ASCII 标点
_PUNCT = "!@#$%^&*()_+-=[]{};:,.<>/?" + "\\|~`'\""
_SCANNED_PAGES = 2
_SCANNED_DPI = 150


def _pick_font() -> Path:
    for name in ("simhei.ttf", "simsun.ttc", "msyh.ttc"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            return p
    raise SystemExit("找不到可嵌入的中文字体（Windows 字体目录）")


def make_scanned(src: Path, dst: Path) -> dict:
    """把源 PDF 的前几页**渲染成图**，再拼成一个没有文字层的 PDF。"""
    src_doc = fitz.open(src)
    out = fitz.open()
    try:
        n = min(_SCANNED_PAGES, src_doc.page_count)
        for i in range(n):
            page = src_doc.load_page(i)
            pix = page.get_pixmap(dpi=_SCANNED_DPI)
            new_page = out.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pix)
    finally:
        src_doc.close()

    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, deflate=True)
    out.close()

    check = fitz.open(dst)
    text = "\n".join(check[i].get_text() for i in range(check.page_count))
    pages = check.page_count
    check.close()
    return {"pages": pages, "extracted_chars": len(text.strip()),
            "chars_per_page": len(text.strip()) / max(pages, 1)}


def make_subset_font(dst: Path) -> dict:
    """造一个"文字层是 ASCII 标点、且用的是子集字体"的 PDF。

    真实现场是：老式中文 PDF 用子集字体把汉字映射到 ASCII 码位，抽出来是
    标点汤。这里用同样的**可观测特征**合成：嵌入字体 → `subset_fonts()`
    把字体切成子集（名字带 `XXXXXX+` 前缀）→ 文字层放 ASCII 标点。
    """
    font_file = _pick_font()
    font = fitz.Font(fontfile=str(font_file))
    doc = fitz.open()
    try:
        for _ in range(2):
            page = doc.new_page()
            tw = fitz.TextWriter(page.rect)
            y = 80.0
            for _row in range(12):
                tw.append((50, y), _PUNCT, font=font, fontsize=12)
                y += 20
            tw.write_text(page)
        # ★ 关键一步：切成子集，字体名会变成 "XXXXXX+SimHei Regular"
        doc.subset_fonts()
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc.save(dst, deflate=True)
    finally:
        doc.close()

    check = fitz.open(dst)
    page = check[0]
    text = page.get_text()
    fonts = [f[3] for f in page.get_fonts()]
    chars = [c for c in text if not c.isspace()]
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    punct = sum(1 for c in chars if c.isascii() and not c.isalnum())
    stats = {
        "pages": check.page_count,
        "fonts": fonts,
        "subset_prefix": all("+" in f for f in fonts),
        "cjk_ratio": round(cjk / max(len(chars), 1), 4),
        "ascii_punct_ratio": round(punct / max(len(chars), 1), 4),
    }
    check.close()
    return stats


def main() -> int:
    corpus = sorted(CORPUS.glob("*.pdf"))
    if not corpus:
        raise SystemExit(f"语料目录没有 PDF: {CORPUS}")
    src = corpus[0]

    scanned = make_scanned(src, OUT / "scanned_sample.pdf")
    print(f"[扫描件] 源={src.name} -> {scanned}")

    subset = make_subset_font(OUT / "subset_font_sample.pdf")
    print(f"[子集字体] -> {subset}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
