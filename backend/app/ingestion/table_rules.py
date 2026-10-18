"""表格单元格的纯规则：多值学分展开（方案 1）。

本模块不依赖 `tables.py`，只接收 `list[str]` 行和表头，方便单测。
"""
from __future__ import annotations

import re

GRADE_TOKEN_RE = re.compile(
    r"(?:特等奖|金奖|银奖|铜奖|优胜奖|[一二三四五](?:等奖)?)$"
)
SCOPE_RE = re.compile(r"(国家级|区级|校级|院级)")
SCOPE_ITEM_RE = re.compile(r"^(?:国家级|区级|校级|院级)$")
CREDIT_RE = re.compile(r"^[\d、.\s]+分[/／]\S*$")
MAX_EXPANSION_RATIO = 5.0


def _split_items(cell: str) -> list[str]:
    return [p.strip() for p in re.split(r"[、,，]", cell) if p.strip()]


def _is_award_item(item: str) -> bool:
    return GRADE_TOKEN_RE.search(re.sub(r"[（）()]", "", item)) is not None


def _is_scope_item(item: str) -> bool:
    return SCOPE_ITEM_RE.match(item.strip()) is not None


def _clean_grade_item(item: str) -> str:
    item = item.strip()
    m = GRADE_TOKEN_RE.search(re.sub(r"[（）()]", "", item))
    if not m:
        return item
    token = m.group(0)
    if token in ("一", "二", "三", "四", "五"):
        return token + "等奖"
    if token in ("一等", "二等", "三等", "四等", "五等"):
        return token + "奖"
    if "（" in item and item.startswith("特等奖"):
        return item
    if item.startswith(token):
        return item
    return token


def _find_scope(*cells: str) -> str:
    for cell in cells:
        m = SCOPE_RE.search(cell or "")
        if m:
            return m.group(1)
    return ""


def _credit_values(cell: str) -> tuple[list[str], str] | None:
    if not CREDIT_RE.match(cell.strip()):
        return None
    vals = [v.strip() for v in re.split(r"[、,，]", cell) if v.strip()]
    if len(vals) < 2:
        return None
    suffixes = {v[v.index("分") + 1:] for v in vals if "分" in v}
    if len(suffixes) != 1:
        return None
    suffix = suffixes.pop()
    values = [v if v.endswith("分" + suffix) else v + "分" + suffix for v in vals]
    values[-1] = vals[-1]
    return values, suffix


def grade_cell_index(raw_row: list[str], credit_idx: int) -> int | None:
    """找学分格左邻 1/2 的等级格；找不到返回 None。"""
    for off in (1, 2):
        gi = credit_idx - off
        if gi < 0:
            break
        cell = raw_row[gi].strip() if gi < len(raw_row) else ""
        items = _split_items(cell)
        if items and all(_is_award_item(x) or _is_scope_item(x) for x in items):
            return gi
    return None


def expand_multi_value_row(raw_row: list[str],
                           col_headers: list[str | None]) -> dict[int, str]:
    """把“多等级 ↔ 多分值”的学分格展开成自包含串。

    返回 `{}` 或 `{学分列下标: "国家级一等奖18分/项；…"}`。
    只看原始行；调用方负责把展开文本接进自然句。
    """
    if "参考学分" not in col_headers:
        return {}
    credit_idx = col_headers.index("参考学分")
    cell = raw_row[credit_idx].strip() if credit_idx < len(raw_row) else ""
    parsed = _credit_values(cell)
    if parsed is None:
        return {}
    values, _suffix = parsed

    gi = grade_cell_index(raw_row, credit_idx)
    if gi is None:
        return {}
    grade_cell = raw_row[gi].strip()
    items = _split_items(grade_cell)
    if len(items) != len(values):
        return {}

    if all(_is_scope_item(x) for x in items):
        scope = ""
    else:
        scope = _find_scope(grade_cell, raw_row[gi - 1] if gi >= 1 else "")

    out_items: list[str] = []
    for item, value in zip(items, values):
        cleaned = _clean_grade_item(item)
        if scope and not cleaned.startswith(scope):
            cleaned = scope + cleaned
        out_items.append(cleaned + value)
    expanded = "；".join(out_items)
    if len(expanded) > len(cell) * MAX_EXPANSION_RATIO:
        return {}
    return {credit_idx: expanded}
