"""表格提取与网格归一（§3.4.1 表格支持）。"""
from __future__ import annotations

from app.ingestion.tables import (
    RawTable, forward_fill, inherit_headers, is_continuation,
    is_projected_row_header, normalize_grid,
)


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


def test_forward_fill_never_shifts_columns_when_first_row_is_short():
    """★回归锁：第 0 列**整列都空**时，绝不能被别的列的值填进来。

    踩过的坑：`last` 没预置到行宽时，`last[i:i+1] = [cell]` 在越界时是**追加**，
    于是 last 整体左移一格 —— 第 1 列的值串进第 0 列。
    实测 doc 12 第 12 页表格首行是 `['', '', '院级', …]`，第 0 列本来就空，
    正是这条路径的触发形状。
    """
    rows = [["", "B", "C"], ["", "E", ""]]
    filled = forward_fill(rows)
    assert [r[0] for r in filled] == ["", ""], "第 0 列整列为空，不该出现任何值"
    assert filled[1][2] == "C", "第 2 列是真合并单元格，应照常承上"


def test_normalize_handles_table_with_no_data_rows():
    """只有表头、没有数据行 —— 读者会期望拿到空数据，而不是崩掉。"""
    rows = [["课程类别", "参考学分"]]
    assert normalize_grid(rows) == [["课程类别", "参考学分"]]


def _t(page, top, bottom, headers, rows):
    """页面按 A4 真实尺寸（595 x 842）构造，别用 800 —— 比例阈值要按真页高算。"""
    return RawTable(page=page, bbox=(0.0, top, 595.0, bottom),
                    headers=list(headers), rows=[list(r) for r in rows])


PH = 842.0            # A4 页高；实测语料就是这个
HDR = ["课程类别", "课程项目", "参考学分"]

# ⚠️ `extract_page_tables` **从不设置 `headers`**（它不做表头判断）。
#    构造续页表时 headers 一律传 `[]` —— 传了值就会走「① 自带表头」分支，
#    根本测不到继承逻辑。


def test_inherit_when_continuation():
    """续页表：锚表伸到页面下部 + 本表从页顶开始 → 继承，首行保留为数据。

    坐标取自 doc 12 的实测值（锚表底 770、本表顶 62）。
    """
    prev = _t(8, 167.2, 769.8, HDR, [["A", "甲", "5分"]])
    cont = _t(9, 62.4, 767.0, [], [["B", "乙", "3分"], ["C", "丙", "1分"]])
    assert is_continuation(cont, prev, page_height=PH)
    out = inherit_headers([cont], prev, page_height=PH)
    assert out[0].headers == HDR
    assert out[0].rows == [["B", "乙", "3分"], ["C", "丙", "1分"]], "首行必须保留为数据"


def test_absolute_pt_threshold_would_have_failed():
    """★回归锁：锚表底 770 距页底 72pt —— 绝对 24pt 容差会判失败。

    本方案第一版写死 `BOTTOM_TOLERANCE=24`，在真实文档上一处都通不过。
    这条测试钉死「必须用比例、不能用绝对 pt」。
    """
    prev = _t(8, 167.2, 769.8, HDR, [["A", "甲", "5分"]])       # 距页底 72.2pt
    cont = _t(9, 62.4, 767.0, [], [["B", "乙", "3分"]])
    assert PH - prev.bbox[3] > 24, "前提：确实超过了 24pt"
    assert is_continuation(cont, prev, page_height=PH), "比例判据必须仍然成立"


def test_no_inherit_when_anchor_not_at_page_bottom():
    """★锚表在半页就结束了 → 不继承。

    这是 RAGFlow PR #18789 的教训：只看「本页表在页顶」就合并，
    会把两张碰巧都在页顶的**独立表**误并，导致第二张表的网格被静默丢弃。
    """
    prev = _t(1, 100, 300, HDR, [["A", "甲", "5分"]])           # 300 < 842×0.8
    nxt = _t(2, 62, 600, [], [["B", "乙", "3分"]])
    assert not is_continuation(nxt, prev, page_height=PH)
    assert inherit_headers([nxt], prev, page_height=PH)[0].headers != HDR


def test_no_inherit_when_table_not_at_page_top():
    """本表不在页顶 → 不是续页。"""
    prev = _t(1, 100, 780, HDR, [["A", "甲", "5分"]])
    nxt = _t(2, 214, 600, [], [["B", "乙", "3分"]])              # doc 11 p8 的实测值
    assert not is_continuation(nxt, prev, page_height=PH)


def test_inherit_across_one_column_difference():
    """★列数差 1 要继承 —— 实测 doc 12 p8(6列) → p9(7列)。

    多出来的那列是「获奖等级」（特等奖/一二三等奖），**承载真实数据**，
    不是空列。「列数必须相等」的判据会把真续页判掉。
    """
    prev = _t(8, 167.2, 769.8, ["课程类别", "课程项目", "课程内容",
                               "参考学分", "开课单位", "获得学分方式"], [["A"] * 6])
    cont = _t(9, 62.4, 767.0, [], [["B"] * 7])
    assert is_continuation(cont, prev, page_height=PH)


def test_no_inherit_when_column_count_far_off():
    """列数差 > 1 → 不继承（列结构对不上，不是同一张表）。"""
    prev = _t(1, 100, 780, HDR, [["A", "甲", "5分"]])            # 3 列
    nxt = _t(2, 62, 600, [], [["B"]])                            # 1 列，差 2
    assert not is_continuation(nxt, prev, page_height=PH)


def test_no_inherit_on_non_adjacent_pages():
    prev = _t(1, 100, 780, HDR, [["A", "甲", "5分"]])
    nxt = _t(4, 62, 600, [], [["B", "乙", "3分"]])
    assert not is_continuation(nxt, prev, page_height=PH)


def test_own_header_wins_over_inheritance():
    """本页表自带表头 → 用自己的，不继承。"""
    prev = _t(1, 100, 780, ["旧列1", "旧列2"], [["A", "甲"]])
    nxt = _t(2, 62, 600, ["新列1", "新列2"], [["新列1", "新列2"], ["B", "乙"]])
    assert inherit_headers([nxt], prev, page_height=PH)[0].headers == ["新列1", "新列2"]


def test_first_table_adopts_its_own_header_row():
    """文档第一张表：没有上文可继承 → 首行采纳为表头并从数据里移除。"""
    first = _t(1, 167.2, 769.8, [], [list(HDR), ["A", "甲", "5分"]])
    out = inherit_headers([first], None, page_height=PH)
    assert out[0].headers == HDR
    assert out[0].rows == [["A", "甲", "5分"]], "表头行不能留在数据里"


def test_single_row_table_is_never_treated_as_header_only():
    """只有一行的表 —— 不能把唯一一行当表头，否则表格变空。"""
    t = _t(1, 0, 780, [], [["课程类别", "参考学分"]])
    out = inherit_headers([t], None, page_height=PH)
    assert out[0].rows == [["课程类别", "参考学分"]], "唯一的行必须留下"


def test_projected_row_header_detection():
    """分组行判定 —— 照抄 PubTables-1M Algorithm 1 第 7 行。"""
    assert is_projected_row_header(["实践实习与社会工作", "", "", "", "", ""], 6)
    assert not is_projected_row_header(["实践实习与社会工作", "劳动教育实践", "", "", "", ""], 6)
    assert not is_projected_row_header(["", "劳动教育实践", "", "", "", ""], 6)
    assert not is_projected_row_header([], 6)


def test_real_doc12_all_continuations_inherit():
    """★真实文档回归：doc 12 的 4 处续页必须全部继承到表头。

    坐标是实测值。锚表距页底 72/75/83/126pt，本表距页顶一律 62.4pt，
    列数 6→7→7→7→7。
    """
    seq = [  # (page, top, bottom, ncols)
        (8, 167.2, 769.8, 6),
        (9, 62.4, 767.0, 7),
        (10, 62.4, 759.2, 7),
        (11, 62.4, 716.0, 7),
        (12, 62.4, 683.6, 7),
    ]
    hdr6 = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
    prev = None
    for i, (pg, top, bot, nc) in enumerate(seq):
        # 首表的首行**就是**它的表头（6 列）；续页表首行是数据（7 列）
        first_row = hdr6 if i == 0 else ["x"] * nc
        t = _t(pg, top, bot, [], [first_row, ["x"] * nc])
        out = inherit_headers([t], prev, page_height=PH)[0]
        assert out.headers == hdr6, f"p{pg} 应拿到表头（首表采纳 / 续页继承）"
        prev = out


def test_real_doc11_independent_tables_never_inherit():
    """★真实文档回归：doc 11 的跨页表对**一对都不能继承**。

    坐标全是实测值。这些表的顶分别在 224 / 136 / 166 / 214 —— **没有一张在页顶**
    （阈值是 842×0.1 = 84.2），所以都是各自独立的表，每张自带头。

    其中 **p5→p6 那对最能说明问题**：锚表底 689 > 842×0.8 = 673.6，**几何上很像
    续页**，靠「本表顶 166 >= 84.2」被挡下 —— 这正是 RAGFlow PR #18789
    假阳性事故的场景，说明**只看锚表贴页底是不够的**。

    （另有一对 p6→p8 锚表底 753 更贴页底，但它连页都不相邻 —— 页 7 没有表 ——
      由「必须相邻页」这条挡下，不重复测。）
    """
    # (前表页, 前表底, 前表列数) → (本表页, 本表顶, 本表列数)
    pairs = [
        ((3, 721, 5), (4, 224, 4)),      # 锚底 721 贴底，但本表顶 224 不在页顶
        ((4, 525, 4), (5, 136, 6)),      # 锚底 525 不贴底
        ((5, 689, 6), (6, 166, 2)),      # ★锚底 689 很贴底，仍被「本表不在页顶」挡下
    ]
    for (pp, pbot, pnc), (cp, ctop, cnc) in pairs:
        prev = _t(pp, 100, pbot, ["列"] * pnc, [["x"] * pnc] * 2)
        cur = _t(cp, ctop, 700, [], [["x"] * cnc] * 2)
        assert not is_continuation(cur, prev, page_height=PH), \
            f"p{pp}→p{cp} 是两张独立表，不该继承"


def test_doc11_anchor_near_bottom_is_not_enough():
    """★单列断言：锚表贴页底**不足以**判定续页 —— 本表位置也必须成立。

    把 p5→p6 那对的「本表顶」单独拎出来，钉死这条几何门控。
    """
    prev = _t(5, 100, 689, ["列"] * 6, [["x"] * 6] * 2)
    assert prev.bbox[3] > PH * 0.8, "前提：锚表确实伸到了页面下部"
    cur = _t(6, 166, 700, [], [["x"] * 2] * 2)
    assert not is_continuation(cur, prev, page_height=PH)
