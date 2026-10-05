"""DOCX 加载器（§3.4.2）。

⚠️ **必须包含表格内容**（附录 B.1.1）：
   旧项目的兜底实现 `"\\n".join(p.text for p in doc.paragraphs)` **丢弃所有表格**——
   制度文档里的表格全部丢失。这里显式遍历 `doc.tables`。

⚠️ `page` **恒为 1**：docx 的分页是渲染产物、不是文档固有属性；
   该字段对 docx 无意义，定位走 4.2.2.4 的 L2/L3（`boxes` 为空 → 从 L2 起步）。

⚠️ 章节来自**标题层级**（不是正则）：docx 的结构是显式的，
   旧项目 docx 被重复解析 5 次（B.2.4），这里一次遍历拿全。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingestion.loaders.base import ChapterMark, LoadResult, PageText

_HEADING_LEVELS = {
    "Heading 1": 1, "Heading 2": 2, "Heading 3": 3,
    "标题 1": 1, "标题 2": 2, "标题 3": 3,
}


def _heading_level(paragraph: Paragraph) -> int | None:
    style = (paragraph.style.name if paragraph.style else "") or ""
    if style in _HEADING_LEVELS:
        return _HEADING_LEVELS[style]
    if style.lower().startswith("heading"):
        digits = "".join(c for c in style if c.isdigit())
        if digits:
            return min(int(digits), 3)
    return None


def _iter_blocks(document: Document):
    """按**文档顺序**产出段落与表格。

    直接 `document.paragraphs` + `document.tables` 会丢掉两者的相对顺序，
    表格会被挪到文末 —— 制度文档里表格的位置本身就是语义。
    """
    from docx.oxml.ns import qn

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _table_to_text(table: Table) -> str:
    lines = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _extract_images(path: Path, out_dir: Path | None) -> list[str]:
    """从 docx 容器里取内嵌图片（word/media/）。"""
    if out_dir is None:
        return []
    saved: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith("word/media/"):
                    continue
                data = zf.read(name)
                if len(data) < 2048:      # 过滤图标类小图
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / Path(name).name
                target.write_bytes(data)
                saved.append(target.name)
    except (zipfile.BadZipFile, OSError):
        return saved
    return saved


def load_docx(path: Path, *, image_dir: Path | None = None) -> LoadResult:
    document = Document(str(path))

    parts: list[str] = []
    heading_specs: list[tuple[str, int]] = []   # (标题, 层级) 按出现顺序
    cursor = 0

    for block in _iter_blocks(document):
        if isinstance(block, Table):
            text = _table_to_text(block)
            if not text:
                continue
        else:
            text = block.text.strip()
            if not text:
                continue
            level = _heading_level(block)
            if level is not None:
                heading_specs.append((text[:60], level))

        if parts:
            parts.append("\n")
            cursor += 1
        parts.append(text)
        cursor += len(text)

    body = "".join(parts)
    images = _extract_images(path, image_dir)

    # 标题 → 章节标记（在拼好的正文里定位）
    marks: list[ChapterMark] = []
    search_from = 0
    for title, level in heading_specs:
        found = body.find(title, search_from)
        if found == -1:
            found = body.find(title)
        if found == -1:
            continue
        marks.append(ChapterMark(chapter=title, level=level, char_offset=found))
        search_from = found + len(title)

    result = LoadResult()
    result.heading_marks = marks
    if body.strip():
        result.pages.append(PageText(page=1, text=body, image_paths=images))
    return result
