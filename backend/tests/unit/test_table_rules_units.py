"""分值列推断与补单位（方案 3 Task 3）。"""
from __future__ import annotations

from app.ingestion.table_rules import apply_score_units, infer_score_columns


def test_infer_score_columns_real_doc11_p3_table0():
    headers = ["文章等级", "第1 作者或通信作者", "第2 作者", "第3 作者"]
    rows = [
        ["SCI", "100", "80", "60"],
        ["EI", "100", "80", "60"],
        ["北大核心", "100", "80", "60"],
    ]
    assert infer_score_columns(rows, headers) == {1, 2, 3}


def test_infer_skips_sequence_and_count_columns():
    headers = ["序号", "人数", "分值"]
    rows = [["1", "20", "90"], ["2", "30", "80"]]
    assert infer_score_columns(rows, headers) == {2}


def test_apply_units_skips_filled_cells():
    raw = ["发明专利申请受理并公开", "50", "30", "10", ""]
    filled = ["发明专利申请受理并公开", "50", "30", "10", "30"]
    mask = [False, False, False, False, True]
    assert apply_score_units(filled, mask, {1, 2, 3, 4}) == [
        "发明专利申请受理并公开", "50分", "30分", "10分", "30",
    ]


def test_apply_units_leaves_parenthetical_and_existing_units():
    raw = ["创业实践", "100（团队其他成员50）", "5 分", "2 分/次"]
    filled = list(raw)
    mask = [False, False, False, False]
    assert apply_score_units(filled, mask, {1, 2, 3}) == raw
