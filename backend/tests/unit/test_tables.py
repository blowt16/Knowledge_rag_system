"""表格提取与网格归一（§3.4.1 表格支持）。"""
from __future__ import annotations

import pytest

from app.ingestion.tables import forward_fill, normalize_grid


def test_normalize_drops_all_empty_column():
    """整列都空的才丢。

    ⚠️ 别拿 doc 12 的 p9 当例子 —— 那一列**不是空的**，它承载真实数据
       （「获奖等级」：特等奖/一二三等奖/优胜奖）。本方案第一版把它误判成
       空列，据此写了「列数应相等」的判据，是错的（见 Task 2 的
       `MAX_COL_DIFF` 注释）。
    """
    rows = [
        ["科技学术与创新创业", "创新创业教育活动", "参与活动", "", "1分/次", "团委"],
        ["", "", "参与培训", "", "5分/期", "团委"],
    ]
    assert normalize_grid(rows) == [
        ["科技学术与创新创业", "创新创业教育活动", "参与活动", "1分/次", "团委"],
        ["", "", "参与培训", "5分/期", "团委"],
    ]


def test_normalize_pads_short_rows():
    """短行右侧补齐 —— 不足的格子补空串，不是补 None。"""
    rows = [["a", "b", "c"], ["d"]]
    assert normalize_grid(rows) == [["a", "b", "c"], ["d", "", ""]]


def test_normalize_keeps_column_that_is_empty_in_some_rows():
    """某列只在部分行为空 —— 不能丢，它可能正是「承上」的合并单元格。"""
    rows = [["a", "b"], ["", "d"]]
    assert normalize_grid(rows) == [["a", "b"], ["", "d"]]


def test_forward_fill_fills_merged_cells():
    """合并单元格向下填充 —— 实测 doc 12 的「课程类别」列连续 16 行只有首行有值。"""
    rows = [
        ["思想政治与道德修养", "社会主义核心价值观教育", "5分/学期"],
        ["", "新生入学教育", "2分/期"],
        ["", "党校培训", "5分/期"],
    ]
    filled = forward_fill(rows)
    assert [r[0] for r in filled] == ["思想政治与道德修养"] * 3
    assert [r[1] for r in filled] == ["社会主义核心价值观教育", "新生入学教育", "党校培训"]


def test_forward_fill_skips_designated_columns():
    """跳过的列不填充 —— 「参考学分」列的空值可能是「承上」的合并，须由调用方决定。"""
    rows = [["a", "x"], ["b", ""]]
    filled = forward_fill(rows, skip_cols=frozenset({1}))
    assert [r[1] for r in filled] == ["x", ""]


def test_forward_fill_leaves_leading_empty_untouched():
    """首行就为空 —— 没有「上文」可承，保持空串，不能填充成别的。"""
    rows = [["", "b"], ["c", "d"]]
    filled = forward_fill(rows)
    assert [r[0] for r in filled] == ["", "c"]


def test_normalize_handles_table_with_no_data_rows():
    """只有表头、没有数据行 —— 读者会期望拿到空数据，而不是崩掉。"""
    rows = [["课程类别", "参考学分"]]
    assert normalize_grid(rows) == [["课程类别", "参考学分"]]
