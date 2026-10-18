"""PDF 表格的提取、表头继承与键值序列化（§3.4.1）。

## 为什么要有这个模块

`page.get_text("blocks")` 会把表格拍平成「一行一格」——单元格变成独立文本行，
行列关系全部丢失。实测《第二课堂成绩单制度实施办法》的评分表：

- 表头（课程类别/课程项目/课程内容/参考学分/开课单位/获得学分方式）只在 chunk 12
- 数据行散布在 chunk 13–24
- **79 条有效数据行里 28 条（35%）的「行标签」与「学分值」被切到不同 chunk**

## 为什么不做跨页合并

RAGFlow PR #18789 的事故：只凭「下一页顶部 y 位移为负」就判定续页表，
把两张碰巧都在页顶的独立表误并，**输出 8 张表而不是 9 张，第二张表的网格被静默丢弃**。
PyMuPDF4LLM 与 Docling 都明确不做跨页合并。

本模块**只做表头继承**：每页表格保持独立对象，一行都不丢。
判据不满足时降级为「无表头」，即当前行为 —— **失败是降级的，不是破坏性的**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ingestion.table_render import render_sentence


#: 表格组的裸哨兵。**不再跟列名**；列名已经在每行自然句里。
#: `clean.py` 靠它豁免清洗，`table_atomic_ranges` 靠它找组边界 —— 都保持不变。
GROUP_MARK = "【表头】"


@dataclass
class RawTable:
    """一页里的一张表。跨页的续页表是**另一个 RawTable**，不做合并。"""
    page: int
    bbox: tuple[float, float, float, float]
    headers: list[str] = field(default_factory=list)   # 空 = 无表头（续页或识别失败）
    rows: list[list[str]] = field(default_factory=list)
    col_spans: list[tuple[float, float]] = field(default_factory=list)  # 每列真实 x 区间
    col_headers: list[str | None] = field(default_factory=list)         # 按列对齐后的表头
    dropped: bool = False                                              # 整表剔除（空白表单）


def _col_spans_from_cells(cells, col_count: int) -> list[tuple[float, float]]:
    """从 PyMuPDF 的 cell 矩形推导每列的 x 区间。

    取所有 cell 的 x0/x1 并集；只有恰好 `col_count + 1` 个边界时才可信。
    合并单元格会让边界数量对不上 —— 这时返回空列表，调用方回退，不猜。
    """
    boxes = [c for c in (cells or []) if c is not None]
    if not boxes or col_count <= 0:
        return []
    xs = sorted(
        {round(float(c[0]), 1) for c in boxes}
        | {round(float(c[2]), 1) for c in boxes}
    )
    if len(xs) != col_count + 1:
        return []
    return [(xs[i], xs[i + 1]) for i in range(col_count)]


def normalize_grid(rows: list[list[str]]) -> list[list[str]]:
    """统一列数：丢弃**全空列**，其余行右侧补齐。

    ⚠️ 只丢「整列都空」的列。某列只在部分行为空是**正常的合并单元格**，
       丢了就把「承上」的语义一起丢了。
    """
    if not rows:
        return []
    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]

    keep = [i for i in range(width) if any(r[i].strip() for r in padded)]
    return [[r[i] for i in keep] for r in padded]


def forward_fill(rows: list[list[str]], *,
                 skip_cols: frozenset[int] = frozenset(),
                 return_mask: bool = False):
    """把合并单元格的值**向下填充**到它覆盖的每一行。

    ⚠️ 实测踩过：只填前两列不够 —— 「开课单位」列同样是合并的。
       默认填所有列，需要保留空值语义的列用 `skip_cols` 排除。

    首行就为空时不填（没有上文可承）。

    `return_mask=True` 时返回 `(filled, mask)`；`mask[i][j]` 表示该格是
    **填充出来的**（原始为空、从上一行承上）。下游做语义处理时不能把
    填充值当成原始值。
    """
    out: list[list[str]] = []
    mask: list[list[bool]] = []
    last: list[str] = []
    for row in rows:
        new = list(row)
        new_mask = [False] * len(new)
        # ⚠️ **必须先把 last 补齐到本行宽度**。否则 `last[i]` 在首行较短时越界，
        #    而 `last[i:i+1] = [cell]` 在越界时的语义是**追加**（不是赋值），
        #    于是后面所有列的「last」整体左移一格 —— 实测：
        #      forward_fill([["", "B", "C"], ["", "E", ""]])
        #      错： [["", "B", "C"], ["B", "E", ""]]   ← B 从第 1 列串到第 0 列
        #      对： [["", "B", "C"], ["", "E", "C"]]
        #    这不是理论风险：doc 12 第 12 页表格的首行就是 `['', '', '院级', …]`，
        #    第 0 列本来就空，正好触发。
        while len(last) < len(new):
            last.append("")
        for i, cell in enumerate(new):
            if i in skip_cols:
                last[i] = cell
                continue
            if cell.strip():
                last[i] = cell
            elif last[i]:
                new[i] = last[i]
                new_mask[i] = True
        out.append(new)
        mask.append(new_mask)
    return (out, mask) if return_mask else out


#: 表头列对齐的 x 容差。实测 p8→p9/p10/p11/p12 的左边界最大差 10.8pt；
#: 新增“获奖等级”列离最近表头 66pt 以上，15pt 足够区分。
X_TOL = 15.0
#: 跳过表头的代价。远大于任何有效匹配，保证“能匹配的都匹配，匹配不上才跳过”。
UNMATCHED_PENALTY = 1000.0
_SCORE_VALUE_RE = re.compile(r"\d+(?:\.\d+)?\s*分")
_DEPT_VALUE_RE = re.compile(r"学院|团委|中心|党委|部（处）|教务处|处")


def _validate_alignment(mapping: list[str | None], rows: list[list[str]]) -> bool:
    """用列内容校验几何映射：分值列要像分值，单位列要像部门。

    ⚠️ 这里只校验两个最硬的锚；不拿“课程内容/获奖等级”做内容猜测。
    """
    checks = (("参考学分", _SCORE_VALUE_RE, 0.9), ("开课单位", _DEPT_VALUE_RE, 0.8))
    for header, pattern, threshold in checks:
        if header not in mapping:
            continue
        idx = mapping.index(header)
        vals = [r[idx].strip() for r in rows if idx < len(r) and r[idx].strip()]
        if vals and sum(bool(pattern.search(v)) for v in vals) / len(vals) < threshold:
            return False
    return True


def align_headers_by_geometry(prev: RawTable, cur: RawTable) -> list[str | None]:
    """把上一张表的表头按列 x 左边界对齐到当前表。

    返回长度 = 当前表列数；`None` 表示当前表新增的无名列。
    不允许匹配超过 `X_TOL` 的列；全部匹配失败时返回 []（调用方回退）。
    """
    # ⚠️ 用 prev.col_headers 而不是 prev.headers：p9 继承后 headers 还是 6 个，
    #    但 col_headers 已经是 7 个（带一个 None）。p10 对齐必须以 7 列为基准。
    if not prev.col_headers or len(prev.col_headers) != len(prev.col_spans):
        return []
    prev_pairs = [(h, span) for h, span in zip(prev.col_headers, prev.col_spans) if h]
    if not prev_pairs:
        return []
    if not cur.col_spans or not cur.rows or len(cur.col_spans) != len(cur.rows[0]):
        return []

    n, m = len(prev_pairs), len(cur.col_spans)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[str, int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if dp[i][j - 1] < dp[i][j]:
                dp[i][j] = dp[i][j - 1]
                back[i][j] = ("skip_col", i, j - 1)
            if dp[i - 1][j] + UNMATCHED_PENALTY < dp[i][j]:
                dp[i][j] = dp[i - 1][j] + UNMATCHED_PENALTY
                back[i][j] = ("skip_hdr", i - 1, j)
            diff = abs(prev_pairs[i - 1][1][0] - cur.col_spans[j - 1][0])
            if diff <= X_TOL and dp[i - 1][j - 1] + diff < dp[i][j]:
                dp[i][j] = dp[i - 1][j - 1] + diff
                back[i][j] = ("match", i - 1, j - 1)

    mapping: list[str | None] = [None] * m
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            j = 0
            break
        if j == 0:
            i = 0
            break
        step = back[i][j]
        if step is None:
            return []
        action, pi, pj = step
        if action == "match":
            mapping[pj] = prev_pairs[pi][0]
        i, j = pi, pj
    if not any(mapping):
        return []
    return mapping if _validate_alignment(mapping, cur.rows) else []


#: 判定「锚表结束在页面下部」—— 用**页高比例**，不用绝对 pt。
#:
#: ⚠️ 2026-10-15 实测：绝对阈值是本方案第一版最大的错误。页面有 **62pt 上边距**，
#:    表格从来不贴页顶/页底。实测 doc 12 的 4 张续页表锚表距页底分别是
#:    72 / 75 / 83 / 126 pt，写死 24pt 一处都通不过。
ANCHOR_BOTTOM_RATIO = 0.8
#: 判定「本表从页顶开始」—— 同样用比例。实测续页表的顶都在 62.4pt ≈ 页高 7.4%。
CONT_TOP_RATIO = 0.1
#: 列数兼容的容差。
#:
#: ⚠️ 不能用「列数相等」：实测同一张逻辑表 doc 12 p8 是 **6 列**、p9–p12 是 **7 列** ——
#:    多的那列是「获奖等级」（特等奖/一二三等奖），**承载真实数据**，不是空列。
#:    第一版误以为它是空列可以丢掉，是错的。
MAX_COL_DIFF = 1


def extract_page_tables(page) -> list[RawTable]:
    """取一页里的所有表格。**不做任何表头判断** —— 交给 `inherit_headers`。"""
    out: list[RawTable] = []
    try:
        found = page.find_tables()
    except Exception:      # noqa: BLE001 —— 表格识别失败不该拖垮整页文本
        return out

    for tab in getattr(found, "tables", []) or []:
        try:
            raw = tab.extract()
        except Exception:  # noqa: BLE001
            continue
        if not raw:
            continue
        bbox = tuple(float(v) for v in tab.bbox)
        out.append(RawTable(
            page=page.number + 1,
            bbox=bbox,
            rows=[
                [(c or "").replace("\n", "").strip() for c in row]
                for row in raw
            ],
            col_spans=_col_spans_from_cells(
                getattr(tab, "cells", None), getattr(tab, "col_count", 0)),
        ))
    return out


def is_continuation(table: RawTable, prev: RawTable | None, *,
                    page_height: float) -> bool:
    """本表是不是上一页那张表的续页。

    **全靠几何，不看文字内容。** 实测三条同时成立才判续页：

    1. 锚表底 > 页高 × `ANCHOR_BOTTOM_RATIO`（锚表伸到页面下部）
    2. 本表顶 < 页高 × `CONT_TOP_RATIO`（本表从页顶开始）
    3. `|列数差| <= MAX_COL_DIFF`（列结构兼容）

    外加两表必须在**相邻页**。

    ⚠️ **为什么不看文字**：2026-10-15 实测，这套判据在语料上打字面——
       doc 12 的 4 处真续页 4/4 通过；doc 11 的 7 对**独立表** 7/7 正确拒绝，
       其中 `p6→p8`（锚表底 753 贴页底、很像续页）因本表顶在 214 而被挡下 ——
       **正是 RAGFlow PR #18789 假阳性事故的场景**。

    ⚠️ **为什么不看字体/底纹**：实测 doc 12 的表头行与数据行在
       字体、字号、flags、颜色、**背景色**上**完全一致**（背景全白、无加粗）。
       doc 11 同样。PyMuPDF `_get_header` 用的加粗/背景色/文本属性那套信号
       在本语料上一条都用不上。
    """
    if prev is None or not prev.headers:
        return False
    if prev.page != table.page - 1:
        return False
    if prev.bbox[3] <= page_height * ANCHOR_BOTTOM_RATIO:
        return False
    if table.bbox[1] >= page_height * CONT_TOP_RATIO:
        return False
    if not table.rows:
        return False
    return abs(len(prev.headers) - len(table.rows[0])) <= MAX_COL_DIFF


def is_projected_row_header(row: list[str], ncols: int) -> bool:
    """这一行是不是「分组行」（projected row header）。

    操作式定义照抄 PubTables-1M（arXiv:2110.00061）Algorithm 1 第 7 行：
    **「不在列头里、且恰好只有一个非空格子、且那个格子在第一个位置」**。
    论文统计 761,262 张受调查表里 153,705 张含这种行（约 20%），不算罕见。

    用途：`serialize_groups` 跳过它 —— 它只是个分类分隔，不承载数据，
    序列化成 `课程类别：实践实习与社会工作` 这种单键行纯属噪声。
    （它的值已经由 `forward_fill` 传播到下面每一行了。）
    """
    if not row or ncols < 2:
        return False
    filled = [i for i, c in enumerate(row) if c.strip()]
    return filled == [0]


def inherit_headers(tables: list[RawTable], prev: RawTable | None, *,
                    page_height: float) -> list[RawTable]:
    """补上表头。**不合并表、不跨页搬行。**

    按**固定顺序**判四档（顺序有意义，别调换）：

    | 档 | 条件 | 结果 |
    |---|---|---|
    | ① | `t.headers` 已有值 | 原样保留 |
    | ② | `is_continuation` 为真 | 用 `prev.headers`；**首行保留为数据** |
    | ③ | 有数据行 | 首行**采纳为表头**，从数据里移除 |
    | ④ | 只有一行 | 不采纳（否则表变空），`headers=[]` 降级 |

    ⚠️ **② 必须排在 ③ 前面。** 续页表的首行是数据，若先走 ③ 会被当表头吃掉。

    ⚠️ ③ **不看文字内容**，只要求「本表自带表头」这个前提成立。
       第一版用「非空格子过半 且 无一格含『数字+分』」的内容启发式，
       被实测否掉了 —— doc 11 的表头行含数字（`第1 作者`、`第2 作者`），
       任何「表头不含数字」的判据都会误判。

    ④ 是**有意的降级**：没有表头只是回到改造前的行为，而错误归类会把
       数据行错标成别的表的列 —— 更糟。
    """
    out: list[RawTable] = []
    for t in tables:
        t.rows = normalize_grid(t.rows)
        if not t.rows:
            out.append(t)
            continue
        ncols = len(t.rows[0])
        # 列数被 normalize_grid 改动过时，列坐标不再可信。
        if len(t.col_spans) != ncols:
            t.col_spans = []

        # ① 自带表头
        if t.headers:
            t.col_headers = list(t.headers) if len(t.headers) == ncols else []
            out.append(t)
            continue

        # ② 续页表 → 继承（首行是数据，留着）
        if is_continuation(t, prev, page_height=page_height):
            t.headers = list(prev.headers)
            t.col_headers = align_headers_by_geometry(prev, t)
            out.append(t)
            continue

        # ③ 本表自带表头行 → 采纳，并从数据里移除
        if len(t.rows) > 1:
            t.headers = list(t.rows[0])
            t.rows = t.rows[1:]
            t.col_headers = list(t.headers) if len(t.headers) == ncols else []
            out.append(t)
            continue

        # ④ 只有一行：不能当表头（表会变空），降级
        t.headers = []
        t.col_headers = []
        out.append(t)
    return out


def serialize_groups(table: RawTable, *, budget: int) -> list[str]:
    """把表格按行分组，每组以裸 `GROUP_MARK` 开头，正文是自然句。

    输出形态（组间由调用方用空行分隔）::

        【表头】
        课程内容是无偿献血，参考学分是2分/次。
        课程内容是捐献造血干细胞，参考学分是10分/次。

    ⚠️ 合并单元格的向下填充在这里做（不在 `inherit_headers` 里）：
       分组行（PRH）的判定必须在填充**之前** —— 一个只填了第一列的 PRH 行，
       一经向下填充就变成满格，再也认不出来。

    ⚠️ 表头的长度先从预算里扣掉，再决定每组放几行。
       事后往切好的片里拼表头必然超预算。

    ⚠️ 单行超过预算时**自成一组**，宁可整组超预算也不切开。
    """
    if not table.rows or table.dropped:
        return []

    ncols = len(table.rows[0])
    # ① 先在**原始行**上判分组行 —— 顺序不能和 ② 换
    prh = [is_projected_row_header(r, ncols) for r in table.rows]
    # ② 再向下填充合并单元格；掩码留给方案 1/3 做语义判断
    filled, _mask = forward_fill(table.rows, return_mask=True)

    # 列名选择：几何对齐的 col_headers 优先；其次自己的表头；都不行才用列号。
    # 列数不符时**绝不按下标配列名**；用 `列N` 自然句，保留每一格。
    if table.col_headers and len(table.col_headers) == ncols:
        col_headers = list(table.col_headers)
    elif len(table.headers) == ncols:
        col_headers = list(table.headers)
    else:
        col_headers = [f"列{i + 1}" for i in range(ncols)]

    body_budget = budget - len(GROUP_MARK) - 1
    lines: list[str] = []
    for raw_row, filled_row, is_prh in zip(table.rows, filled, prh):
        if is_prh:
            # 分组行只是分类分隔，不承载数据；它的值已被 forward_fill 传播。
            continue
        sentence = render_sentence(raw_row, filled_row, col_headers)
        if sentence:
            lines.append(sentence)

    if not lines:
        return []

    groups: list[str] = []
    cur: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1                     # +1 = 换行
        if cur and used + cost > body_budget:
            groups.append(GROUP_MARK + "\n" + "\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += cost
    if cur:
        groups.append(GROUP_MARK + "\n" + "\n".join(cur))
    return groups


def table_atomic_ranges(normalized: str) -> list[tuple[int, int]]:
    """扫描规范化文本，返回每个表格组的字符区间 —— 交给 `chunk_text(atomic=)`。

    组 = 以 `GROUP_MARK` 开头、到**下一个空行**为止的块。
    `serialize_groups` 保证组内不出现空行、组间恰好空一行（`pdf.py` 插组时
    前后各补一个空行并把连续空行收敛成一行），所以「到下一个空行」等价于
    「到本组结束」—— 后面那 2 格空白落在区间外，正文进来不了。

    ⚠️ **游标必须前进，不能对每组重跑 `find(组文本)`**：
       一串重复行被预算切在中间时，相邻两组可以序列化出**逐字节相同**的
       文本。从头 `find` 会把两组映射到同一个区间 —— 而 `chunk_text` 的命中
       判据是 `s <= pos < e` 且**不合并重叠区间**：重叠的那段文本会被吐出两次，
       真正漏掉的那组反而完全不受保护（实测：第 2 组被切碎，行值跨 chunk 重复
       出现）。这里扫的是**唯一**的 `【表头】` 标记（格子内的 `【` 已被
       `_escape` 转义，正文里不存在裸标记），游标严格越过上一个标记
       （`blank > start`，且 `【` 不等于换行），所以区间天然递增、两两不重叠。

    ⚠️ `end` 取 `blank`（空行前那个 `\\n` 的下标），**不取 `blank + 2`**：
       区间是「这一组的字符」，`text[line_start:blank]` 恰好就是；
       `blank + 2` 会把空行也圈进来 —— 而在本管线里那两格往往正是
       `build_normalized_text` 的**页间分隔符**（`parts.append("\\n\\n")`）。
       实测 doc 12：两种取法产出的正文逐字相同（`_emit` 会 `strip()`）、
       页码归属也不变（`enrich` 用 `char_end - 1`，`page_of_offset` 对页间
       分隔符有回落）—— 也就是说 `+2` 没有收益，只让区间不再等于「这一组」。
       边界取精确的那个，语义才立得住。
    """
    out: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = normalized.find(GROUP_MARK, pos)
        if start == -1:
            break
        # 回到行首（GROUP_MARK 前可能缩进）
        line_start = normalized.rfind("\n", 0, start) + 1
        # 找下一个空行：组内无空行，所以它就是本组的结束
        blank = normalized.find("\n\n", start)
        end = blank if blank != -1 else len(normalized)
        out.append((line_start, end))
        pos = end
    return out
