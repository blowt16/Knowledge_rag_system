"""文本清洗与规范化文本写出（§3.4.1）。

⚠️ 清洗**必须在竖排检测之后**（§E.3.3）：清洗会先删掉「成文日期」这类单字符行，
   反转再也救不回来。实测文档 06 首页的 `'9'` / `'8'` / `'2019'` 三条都命中
   `^\\d{1,4}$` 规则 —— 若先清洗再反转，会得到「桂林电子科技大学**年 月 日**」，
   成文日期被毁掉。

产出物：`normalized_text` —— 它就是 `char_start/char_end` 的**参照系**
（§3.3.2：偏移相对清洗后的规范化文本，不是原始 PDF 的字节偏移）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# 独立数字行（页码）：`^\\d{1,4}$`
_PAGE_NUMBER_RE = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")
# 目录行：`第一章 ................ 3`
_TOC_RE = re.compile(r"^.{0,60}?[.·…]{4,}\s*\d{1,4}\s*$")
# 连续空白
_MULTI_SPACE_RE = re.compile(r"[ \t　]{2,}")
# 连续空行
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


@dataclass
class PageSlice:
    page: int
    char_start: int
    char_end: int


def _strip_control(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs"):
            continue
        out.append(ch)
    return "".join(out)


def find_repeated_lines(pages: list[str], *, min_ratio: float = 0.6,
                        max_len: int = 40) -> set[str]:
    """找出跨页重复的短行 —— 页眉、页脚、红头。

    只在「出现在足够多页」且**行本身很短**时才判为页眉页脚，
    避免把「第一章 总则」这类正常短标题误删。
    """
    if len(pages) < 3:
        return set()
    counter: Counter[str] = Counter()
    for text in pages:
        # 一页内重复出现只算一次
        for line in {ln.strip() for ln in text.split("\n") if ln.strip()}:
            counter[line] += 1
    threshold = max(2, int(len(pages) * min_ratio))
    return {line for line, count in counter.items()
            if count >= threshold and len(line) <= max_len}


def clean_page(text: str, *, repeated: set[str] | None = None) -> str:
    """清洗单页。规则：控制字符 / 页眉页脚 / 页码行 / 目录行。"""
    text = _strip_control(text)
    repeated = repeated or set()

    kept: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            kept.append("")
            continue
        if stripped in repeated:
            continue
        if _PAGE_NUMBER_RE.match(stripped):
            continue
        if _TOC_RE.match(stripped):
            continue

        kept.append(_MULTI_SPACE_RE.sub(" ", line))

    result = "\n".join(kept)
    result = _MULTI_BLANK_RE.sub("\n\n", result)
    return result.strip()


def build_normalized_text(page_texts: list[tuple[int, str]]) -> tuple[str, list[PageSlice]]:
    """把各页清洗后的文本拼成规范化文本，并记录每页的字符区间。

    `page_texts` 是 (页码, 原始页文本) 的列表，**页文本必须已经做过竖排恢复**。

    返回 `(normalized_text, page_slices)`；
    `page_slices` 让 chunk 的字符偏移能反查出页码（`page` metadata 的来源）。
    """
    raw_pages = [text for _, text in page_texts]
    repeated = find_repeated_lines(raw_pages)

    parts: list[str] = []
    slices: list[PageSlice] = []
    cursor = 0

    for page_no, raw in page_texts:
        cleaned = clean_page(raw, repeated=repeated)
        if not cleaned:
            continue
        if parts:
            parts.append("\n\n")   # 页间分隔
            cursor += 2
        start = cursor
        parts.append(cleaned)
        cursor += len(cleaned)
        slices.append(PageSlice(page=page_no, char_start=start, char_end=cursor))

    return "".join(parts), slices


def page_of_offset(slices: list[PageSlice], offset: int) -> int:
    """字符偏移 → 页码。落在页间分隔符上时归到前一页。"""
    for s in slices:
        if s.char_start <= offset < s.char_end:
            return s.page
    # 落在两页之间的分隔符里
    previous = slices[0].page if slices else 1
    for s in slices:
        if offset < s.char_start:
            return previous
        previous = s.page
    return previous
