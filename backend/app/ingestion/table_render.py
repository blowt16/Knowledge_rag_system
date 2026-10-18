"""表格行的自然句渲染。

doc 12 的 6 列评分表用专用模板；其他表格用通用“列名是值”的句子。

⚠️ 本模块不导入 `tables.py`，避免循环依赖；只接收 list 行和表头。
"""
from __future__ import annotations

DOC12_HEADERS = {
    "课程类别", "课程项目", "课程内容",
    "参考学分", "开课单位", "获得学分方式",
}
PLACEHOLDERS = {"", "其他", "无", "—", "-", "／", "/"}
_ESCAPE = {"｜": "丨", "【": "〔", "】": "〕"}


def _escape(cell: str) -> str:
    for bad, good in _ESCAPE.items():
        cell = cell.replace(bad, good)
    return cell


def _value(row: list[str], headers: list[str | None], name: str) -> str:
    if name not in headers:
        return ""
    i = headers.index(name)
    return _escape(row[i].strip()) if i < len(row) else ""


def render_sentence(raw_row: list[str], filled_row: list[str],
                    col_headers: list[str | None]) -> str:
    if DOC12_HEADERS.issubset({h for h in col_headers if h}):
        return _render_doc12(raw_row, filled_row, col_headers)
    return _render_generic(filled_row, col_headers)


def _render_doc12(raw_row: list[str], filled_row: list[str],
                  col_headers: list[str | None]) -> str:
    cat = _value(filled_row, col_headers, "课程类别")
    proj = _value(filled_row, col_headers, "课程项目")
    content = _value(filled_row, col_headers, "课程内容")
    score = _value(filled_row, col_headers, "参考学分")
    unit = _value(filled_row, col_headers, "开课单位")
    way = _value(filled_row, col_headers, "获得学分方式")
    detail_idx = next((i for i, h in enumerate(col_headers) if h is None), None)
    detail = _escape(raw_row[detail_idx].strip()) if (
        detail_idx is not None and detail_idx < len(raw_row)) else ""

    head = " - ".join(p for p in (cat, proj) if p)
    if content:
        if detail_idx is None:
            head = f"{head}：{content}" if head else content
        else:
            head = f"{head} - {content}" if head else content
    if detail and detail not in PLACEHOLDERS:
        head += f"：{detail}"
    if score:
        head += f"，参考学分 {score}"
    if unit:
        head += f"，{unit}"
    if way:
        head += f"（{way}）"
    return head + "。" if head else ""


def _render_generic(filled_row: list[str],
                    col_headers: list[str | None]) -> str:
    parts: list[str] = []
    for i, h in enumerate(col_headers):
        value = filled_row[i].strip() if i < len(filled_row) else ""
        if not value:
            continue
        parts.append(f"{h or f'列{i + 1}'}是{_escape(value)}")
    return "，".join(parts) + "。" if parts else ""
