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

from dataclasses import dataclass, field


#: 序列化用的分隔符。
#:
#: ⚠️ **只有 `SEP` 需要避开切分器的断句字符**（`chunker._SENTENCE_END` /
#:    `_SECONDARY_END`）—— 它是列间分隔，落在断句字符上会让 `_best_break`
#:    从中间断开。`｜`(U+FF5C) 不在那两个集合里，安全。
#:
#: ⚠️ `KV`(`：`) 和 `GROUP_MARK` 的尾字符 `】` **确实在 `_SECONDARY_END` 里**
#:    （实测：`_SECONDARY_END = "，、：)）】」』"`）。**这不影响正确性** ——
#:    表格组不被切开是靠 Task 4 的 `atomic` 区间保证的，那里是硬边界；
#:    断句字符只影响组**内部**怎么断，而原子区间根本不给它断的机会。
#:    别看到这里就以为 KV 需要换 —— 换掉才会破坏「列名：值」的可读性。
SEP = "｜"          # U+FF5C 全角竖线
KV = "："           # U+FF1A 全角冒号
GROUP_MARK = "【表头】"


@dataclass
class RawTable:
    """一页里的一张表。跨页的续页表是**另一个 RawTable**，不做合并。"""
    page: int
    bbox: tuple[float, float, float, float]
    headers: list[str] = field(default_factory=list)   # 空 = 无表头（续页或识别失败）
    rows: list[list[str]] = field(default_factory=list)


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
                 skip_cols: frozenset[int] = frozenset()) -> list[list[str]]:
    """把合并单元格的值**向下填充**到它覆盖的每一行。

    ⚠️ 实测踩过：只填前两列不够 —— 「开课单位」列同样是合并的。
       默认填所有列，需要保留空值语义的列用 `skip_cols` 排除。

    首行就为空时不填（没有上文可承）。
    """
    out: list[list[str]] = []
    last: list[str] = []
    for row in rows:
        new = list(row)
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
        out.append(new)
    return out


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
        out.append(RawTable(page=page.number + 1, bbox=bbox, rows=[
            [(c or "").replace("\n", "").strip() for c in row] for row in raw
        ]))
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

        # ① 自带表头
        if t.headers:
            out.append(t)
            continue

        # ② 续页表 → 继承（首行是数据，留着）
        if is_continuation(t, prev, page_height=page_height):
            t.headers = list(prev.headers)
            out.append(t)
            continue

        # ③ 本表自带表头行 → 采纳，并从数据里移除
        if len(t.rows) > 1:
            t.headers = list(t.rows[0])
            t.rows = t.rows[1:]
            out.append(t)
            continue

        # ④ 只有一行：不能当表头（表会变空），降级
        t.headers = []
        out.append(t)
    return out


#: 单元格内容里出现这些字符时的替身 —— 保证正文里**不出现裸的** SEP / GROUP_MARK。
_ESCAPE = {"｜": "丨", "【": "〔", "】": "〕"}


def _escape(cell: str) -> str:
    for bad, good in _ESCAPE.items():
        cell = cell.replace(bad, good)
    return cell


def serialize_groups(table: RawTable, *, budget: int) -> list[str]:
    """把表格按行分组，每组自带表头。

    输出形态（每组是一段文本，组间由调用方用空行分隔）::

        【表头】课程内容｜参考学分
        课程内容：无偿献血｜参考学分：2分/次
        课程内容：捐献造血干细胞｜参考学分：10分/次

    ⚠️ **合并单元格的向下填充在这里做**（不在 `inherit_headers` 里）：
       分组行（PRH）的判定必须在填充**之前** —— 一个只填了第一列的 PRH 行，
       一经向下填充就变成满格，再也认不出来。

    ⚠️ **表头的长度先从预算里扣掉**，再决定每组放几行。
       Docling #3428 / LightRAG HeaderRecovery 都是这个做法 ——
       事后往切好的片里拼表头必然超预算。

    ⚠️ 单行超过预算时**自成一组**，宁可整组超预算也不切开 ——
       切开就等于回到改造前「标签与值分离」的老问题。
    """
    if not table.rows:
        return []

    ncols = len(table.rows[0])
    # ① 先在**原始行**上判分组行 —— 顺序不能和 ② 换
    prh = [is_projected_row_header(r, ncols) for r in table.rows]
    # ② 再向下填充合并单元格
    filled = forward_fill(table.rows)

    headers = ([_escape(h) for h in table.headers]
               if table.headers else [f"列{i + 1}" for i in range(ncols)])
    header_line = GROUP_MARK + SEP.join(headers)
    # 扣掉表头 + 一个换行
    body_budget = budget - len(header_line) - 1

    lines: list[str] = []
    for row, is_prh in zip(filled, prh):
        if is_prh:
            # 分组行只是分类分隔，不承载数据；它的值已被 ② 传播到下面每一行。
            # 序列化成 `课程类别：实践实习与社会工作` 这种单键行纯属噪声。
            continue
        pairs = [f"{headers[i]}{KV}{_escape(row[i])}"
                 for i in range(min(len(headers), len(row))) if row[i].strip()]
        if pairs:
            lines.append(SEP.join(pairs))

    if not lines:
        return []

    groups: list[str] = []
    cur: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1                     # +1 = 换行
        if cur and used + cost > body_budget:
            groups.append(header_line + "\n" + "\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += cost
    if cur:
        groups.append(header_line + "\n" + "\n".join(cur))
    return groups
