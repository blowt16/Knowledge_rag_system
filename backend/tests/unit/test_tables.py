"""表格提取与网格归一（§3.4.1 表格支持）。"""
from __future__ import annotations

from pathlib import Path

import fitz

from app.ingestion.tables import (
    RawTable, _col_spans_from_cells, align_headers_by_geometry,
    extract_page_tables, forward_fill, inherit_headers,
    is_continuation, is_projected_row_header, normalize_grid,
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


from app.ingestion.tables import GROUP_MARK, serialize_groups


def _tbl(headers, rows):
    return RawTable(page=1, bbox=(0.0, 0.0, 100.0, 100.0), headers=list(headers), rows=rows)


def test_every_row_carries_its_column_names():
    """★核心断言：序列化后每一行都自带完整列名，切到哪都不丢归属。"""
    t = _tbl(["课程内容", "参考学分"], [["无偿献血", "2分/次"]])
    groups = serialize_groups(t, budget=500)
    assert groups[0].splitlines()[1] == "课程内容是无偿献血，参考学分是2分/次。"


def test_group_starts_with_bare_marker_line():
    t = _tbl(["参考学分"], [["2分/次"]])
    assert serialize_groups(t, budget=500)[0].splitlines()[0] == GROUP_MARK


def test_mismatched_header_count_never_mislabels_columns():
    """★列数不符时**绝不按下标配列名** —— 这是本项目要消灭的失效类型。

    实测 doc 12 的 p9–p12：继承来的表头 6 列、数据行 7 列
    （多的「获奖等级」列承载真实数据）。按下标硬配的实际输出是：
        …｜课程内容：无偿献血｜开课单位：2分/次｜获得学分方式：团委、各学院
    —— 学分值被挂到「开课单位」名下，「参考学分」消失，第 7 格被静默丢弃，
    影响 83 行。**「自信的错误标注」比拍平文本更糟**，因为拍平不会说谎。

    多出来的列在**中间**、不在尾部，真实对齐无法从数据恢复 —— 所以不猜。
    """
    t = _tbl(["课程类别", "课程内容", "参考学分"],
             [["甲类", "无偿献血", "2分/次", "获奖等级值"]])
    body = serialize_groups(t, budget=500)[0]

    assert "列1是甲类" in body, "应逐格按位置标注"
    assert "列4是获奖等级值" in body, "第 4 格必须保留，不得被静默丢弃"
    assert "参考学分是2分/次" not in body, "值不得挂到错位的列名上"
    assert "课程内容是无偿献血" not in body, "同上：下标配对整体错位"


def test_matching_header_count_still_pairs_by_name():
    """对照：列数相符时仍按列名配对 —— 别把上面那条修过头了。"""
    t = _tbl(["课程内容", "参考学分"], [["无偿献血", "2分/次"]])
    body = serialize_groups(t, budget=500)[0].splitlines()[1]
    assert body == "课程内容是无偿献血，参考学分是2分/次。"


def test_header_tokens_are_deducted_before_grouping():
    """★表头先扣预算：每组**含表头**不得超过 budget。

    Docling #3428 的 bug 就是没扣，切完再拼表头 → 超 max_tokens。
    """
    rows = [[f"行{i}", "1分/次"] for i in range(50)]
    t = _tbl(["课程内容", "参考学分"], rows)
    groups = serialize_groups(t, budget=200)
    for g in groups:
        assert len(g) <= 200, f"组超预算：{len(g)}"


def test_oversized_single_row_becomes_its_own_group():
    """★单个格子比预算还长 —— 自成一组且**不被切开**（否则死循环）。"""
    long_cell = "长" * 400
    t = _tbl(["参考学分"], [[long_cell], ["2分/次"]])
    groups = serialize_groups(t, budget=200)
    assert any(long_cell in g for g in groups), "超长行必须完整出现在某一组里"


def test_escape_cell_separators():
    """★单元格里含分隔符或标记 —— 转义后不破坏结构，扫描器不误判。

    ⚠️ 列名数必须与格子数对齐，否则第二个格子根本不会被序列化，
       断言就成了空转（改这条测试时踩过）。
    """
    t = _tbl(["甲列", "乙列"], [["含｜竖线的值", "含【表头】标记的值"]])
    lines = serialize_groups(t, budget=500)[0].splitlines()
    body = "\n".join(lines[1:])
    assert GROUP_MARK not in body, "正文里不能出现裸的组标记"
    assert "｜" not in body, "格子内的 ｜ 必须被转义"
    assert "【" not in body and "】" not in body, "格子内的 【】 必须被转义"


def test_no_data_rows_yields_no_groups():
    t = _tbl(["参考学分"], [])
    assert serialize_groups(t, budget=500) == []


def test_projected_row_header_is_skipped_but_its_value_propagates():
    """★分组行（PRH）不单独成行，但它的值要填充到下面每一行。

    照 PubTables-1M 的定义，「只填第一列」的行是分类分隔（实践实习与社会工作
    这类），不承载数据。若照常序列化，会产出一条只有 `课程类别：xxx` 的噪声行。
    """
    t = _tbl(["课程类别", "课程内容", "参考学分"], [
        ["实践实习与社会工作", "", ""],          # PRH
        ["", "无偿献血", "2分/次"],
        ["", "捐献造血干细胞", "10分/次"],
    ])
    lines = serialize_groups(t, budget=500)[0].splitlines()
    assert len(lines) == 3, "裸哨兵 + 2 条数据行；PRH 不该单独成行"
    assert lines[1] == "课程类别是实践实习与社会工作，课程内容是无偿献血，参考学分是2分/次。"
    assert lines[2].startswith("课程类别是实践实习与社会工作")


def test_forward_fill_does_not_swallow_first_data_row():
    """填充不能把真实数据行吃掉 —— 首行有数据的行照常输出。"""
    t = _tbl(["课程类别", "参考学分"], [["甲类", "5分"], ["", "3分"]])
    lines = serialize_groups(t, budget=500)[0].splitlines()
    assert len(lines) == 3
    assert lines[1] == "课程类别是甲类，参考学分是5分。"


from app.ingestion.tables import table_atomic_ranges


def test_atomic_ranges_cover_each_group_block():
    """每个【表头】开头的块 → 一个原子区间，到下一个空行为止。"""
    text = "正文。\n\n【表头】A｜B\nA：1｜B：2\n\n【表头】A｜B\nA：3｜B：4\n\n正文继续。"
    ranges = table_atomic_ranges(text)
    assert len(ranges) == 2
    for s, e in ranges:
        assert text[s:e].startswith(GROUP_MARK)


def test_atomic_ranges_do_not_swallow_following_prose():
    """★区间必须止于块尾，不能把后面的正文一起吃进去。"""
    text = "【表头】A\nA：1\n\n正文在这里。"
    (s, e), = table_atomic_ranges(text)
    assert "正文在这里" not in text[s:e]


def test_atomic_ranges_empty_when_no_table():
    assert table_atomic_ranges("纯正文，没有表格。") == []


def test_atomic_ranges_stay_distinct_for_identical_groups():
    """★相邻两组序列化成**逐字节相同**的文本时，区间必须各自独立。

    重复行被预算切在中间时，相邻两组可以是同一个字符串（同一条表头行、
    同样的若干行数据）。若解析器对每组都从 0 重跑 `find(组文本)`，
    两组会映射到**同一个区间** —— 而 `chunk_text` 的命中判据是
    `s <= pos < e` 且**不合并重叠区间**：重叠区间会让那段文本被吐出两次，
    真正漏掉的那组则根本不受保护。所以扫描必须靠**游标前进**。
    """
    group = "【表头】A｜B\nA：1｜B：2"
    text = "前文。\n\n" + group + "\n\n" + group + "\n\n后文。"
    (s1, e1), (s2, e2) = table_atomic_ranges(text)
    assert s1 != s2, "两组必须映射到不同区间，不能都指回第一组"
    assert e1 <= s2, "区间必须递增且两两不重叠"
    assert text[s1:e1] == text[s2:e2] == group


# ---- 方案乙 Task 1：列坐标 + 填充掩码 --------------------------------

def test_col_spans_from_real_p9_cells():
    cells = [
        (30.7, 0, 66.4, 1),
        (66.4, 0, 158.5, 1),
        (158.5, 0, 225.3, 1),
        (225.3, 0, 313.0, 1),
        (313.0, 0, 371.1, 1),
        (371.1, 0, 442.3, 1),
        (442.3, 0, 564.5, 1),
    ]
    assert _col_spans_from_cells(cells, 7) == [
        (30.7, 66.4), (66.4, 158.5), (158.5, 225.3), (225.3, 313.0),
        (313.0, 371.1), (371.1, 442.3), (442.3, 564.5),
    ]


def test_col_spans_rejects_inconsistent_grid():
    cells = [(0, 0, 10, 1), (10, 0, 20, 1)]
    assert _col_spans_from_cells(cells, 3) == []


def test_forward_fill_returns_mask():
    rows = [["a", "x"], ["b", ""]]
    filled, mask = forward_fill(rows, return_mask=True)
    assert filled == [["a", "x"], ["b", "x"]]
    assert mask == [[False, False], [False, True]]


# ---- 方案乙 Task 2：几何表头对齐 --------------------------------

P8 = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
P8_SPANS = [(30.7, 66.4), (66.4, 158.5), (158.5, 312.0),
            (312.0, 365.1), (365.1, 439.5), (439.5, 564.5)]
P9_SPANS = [(30.7, 66.4), (66.4, 158.5), (158.5, 225.3),
            (225.3, 313.0), (313.0, 371.1), (371.1, 442.3), (442.3, 564.5)]
P10_SPANS = [(30.6, 66.4), (66.4, 158.5), (158.5, 227.0),
             (227.0, 307.3), (307.3, 371.1), (371.1, 442.3), (442.3, 564.5)]

DOC12 = Path(__file__).resolve().parents[3] / (
    "corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf")


def _geo_t(headers, spans, rows=None):
    return RawTable(page=1, bbox=(0, 0, 0, 0), headers=list(headers),
                    col_headers=list(headers),
                    rows=rows or [[""] * len(spans)], col_spans=list(spans))


def test_align_doc12_p9():
    prev = _geo_t(P8, P8_SPANS)
    cur = _geo_t([], P9_SPANS)
    assert align_headers_by_geometry(prev, cur) == [
        "课程类别", "课程项目", "课程内容", None,
        "参考学分", "开课单位", "获得学分方式",
    ]


def test_align_doc12_p10():
    prev = _geo_t(P8, P8_SPANS)
    cur = _geo_t([], P10_SPANS)
    mapping = align_headers_by_geometry(prev, cur)
    assert mapping[3] is None
    assert mapping[4] == "参考学分"


def test_align_rejects_out_of_tolerance():
    prev = _geo_t(P8, P8_SPANS)
    # 整体平移仍可能让某一列偶然落在 15pt 内（列宽不同）；用完全在远处的
    # 坐标，确保一个都配不上。
    cur = _geo_t([], [(1000 + i * 100, 1050 + i * 100) for i in range(7)])
    assert align_headers_by_geometry(prev, cur) == []


def test_real_doc12_col_headers_align():
    doc = fitz.open(DOC12)
    prev = None
    try:
        for page_no in range(8, 13):
            page = doc.load_page(page_no - 1)
            tables = inherit_headers(
                extract_page_tables(page), prev, page_height=page.rect.height)
            t = tables[0]
            if page_no == 8:
                assert t.col_headers == P8
            else:
                assert t.col_headers == [
                    "课程类别", "课程项目", "课程内容", None,
                    "参考学分", "开课单位", "获得学分方式",
                ]
            prev = t
    finally:
        doc.close()


# ---- 方案乙 Task 4：自然句序列化 --------------------------------

def test_group_starts_with_bare_marker():
    t = _tbl(["课程内容", "参考学分"], [["无偿献血", "2分/次"]])
    group = serialize_groups(t, budget=500)[0]
    assert group.splitlines()[0] == GROUP_MARK, "哨兵不能再带列名"


def test_rows_are_natural_sentences():
    t = _tbl(["课程内容", "参考学分"], [["无偿献血", "2分/次"]])
    body = serialize_groups(t, budget=500)[0]
    assert "课程内容是无偿献血" in body
    assert "参考学分是2分/次" in body
    assert "｜" not in body


def test_mismatched_header_count_uses_position_labels():
    t = _tbl(["课程类别", "课程内容", "参考学分"],
             [["甲类", "无偿献血", "2分/次", "获奖等级值"]])
    body = serialize_groups(t, budget=500)[0]
    assert "列1是甲类" in body
    assert "列4是获奖等级值" in body
    assert "参考学分是2分/次" not in body, "列数不符时不得按名字错配"
