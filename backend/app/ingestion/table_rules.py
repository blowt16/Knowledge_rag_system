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

#: 空白申请表的字段词。**单靠它们会误伤**，必须再加结构信号。
FORM_WORDS = ("审核人签字", "学号", "姓名", "合计", "专业", "课号")
EXAMPLE_RE = re.compile(r"^例\d+$")


PURE_INT = re.compile(r"^\d+$")
#: 这些表头下的整数不是分数；整列跳过，不补“分”。
NON_SCORE_HEADERS = ("序号", "编号", "人数", "年份", "数量", "页码")


def infer_score_columns(rows: list[list[str]],
                        headers: list[str | None]) -> set[int]:
    """按列推断哪些列是分值列。

    列内 ≥80% 的非空值是纯整数，且都在 0–1000 —— 视为分值列。
    表头命中 `NON_SCORE_HEADERS` 的整列跳过。
    """
    if not rows:
        return set()
    ncols = len(rows[0])
    out: set[int] = set()
    for i in range(ncols):
        header = headers[i] if i < len(headers) and headers[i] else ""
        if any(w in header for w in NON_SCORE_HEADERS):
            continue
        vals = [r[i].strip() for r in rows if i < len(r) and r[i].strip()]
        if not vals:
            continue
        ints = [v for v in vals if PURE_INT.match(v)]
        if len(ints) / len(vals) < 0.8:
            continue
        if all(0 <= int(v) <= 1000 for v in ints):
            out.add(i)
    return out


def apply_score_units(filled_row: list[str], mask_row: list[bool],
                      score_cols: set[int]) -> list[str]:
    """给分值列里的裸整数补“分”；`forward_fill` 填出来的格子跳过。"""
    out = list(filled_row)
    for i in score_cols:
        if i >= len(out) or i >= len(mask_row) or mask_row[i]:
            continue
        value = out[i].strip()
        if PURE_INT.match(value):
            out[i] = value + "分"
    return out


def is_application_form(rows: list[list[str]]) -> bool:
    """识别“空白申请表”模板：表头词命中 ≥2，且至少两个结构信号。

    结构信号：首行空表头 ≥4、例N 行 ≥3、有“合计”行。
    只命中字段词、没有结构信号的正常表不剔除。
    """
    if not rows:
        return False
    first = rows[0]
    flat = ["".join(c.split()) for r in rows for c in r]
    word_hits = sum(1 for w in FORM_WORDS if any(w in x for x in flat if x))
    empty_header_cells = sum(1 for c in first if not c.strip())
    example_rows = sum(1 for r in rows if r and EXAMPLE_RE.match(r[0].strip()))
    has_total = any(r and r[0].strip() == "合计" for r in rows)
    structural = sum([
        empty_header_cells >= 4,
        example_rows >= 3,
        has_total,
    ])
    return word_hits >= 2 and structural >= 2


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
