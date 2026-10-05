"""Markdown 加载器（§3.4.2）。

用 **mistune** 的 AST 提取目录结构 —— 不是 `markdown` 库（那个是「Markdown → HTML」，
旧项目装了但全仓零 import，且已列入 C.2.2 的删除清单）。

⚠️ `page` **恒为 1**（与 docx 同理）。

⚠️ 旧项目 `md_parser.py` 与 `file_handler.py` 各有一份**逐字重复**的 TOC 层级算法
   （附录 B.2.4），这里只保留一份。
"""

from __future__ import annotations

from pathlib import Path

import mistune

from app.core.config import cfg
from app.ingestion.loaders.base import ChapterMark, LoadResult, PageText


def _decode(path: Path) -> str:
    from app.ingestion.file_type import detect_encoding

    return path.read_text(encoding=detect_encoding(path), errors="replace")


def _heading_marks(text: str) -> list[ChapterMark]:
    """从 AST 抽标题，并回正文里定位其偏移。"""
    md = mistune.create_markdown(renderer=None)
    tokens = md(text)

    marks: list[ChapterMark] = []
    search_from = 0

    def walk(nodes) -> None:
        nonlocal search_from
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "heading":
                level = int(node.get("attrs", {}).get("level", 1))
                title = _text_of(node.get("children", [])).strip()
                if title:
                    # 标题在正文里唯一，从上次位置往后找，找不到则从头找
                    found = text.find(title, search_from)
                    if found == -1:
                        found = text.find(title)
                    if found != -1:
                        marks.append(ChapterMark(
                            chapter=title[:60],
                            level=min(level, 3),
                            char_offset=found,
                        ))
                        search_from = found + len(title)
            walk(node.get("children"))

    walk(tokens)
    return marks


def _text_of(nodes) -> str:
    out: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "text":
            out.append(node.get("raw", ""))
        else:
            out.append(_text_of(node.get("children", [])))
    return "".join(out)


def load_md(path: Path, **_kwargs) -> LoadResult:
    text = _decode(path)

    result = LoadResult()
    result.heading_marks = _heading_marks(text)
    if text.strip():
        result.pages.append(PageText(page=1, text=text))
    return result
