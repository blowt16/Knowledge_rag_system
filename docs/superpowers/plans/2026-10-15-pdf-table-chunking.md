# PDF 表格切分修复 — 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让 PDF 里的表格在切分后，每一条数据行都保留自己的列名归属，且表格不被从中间切断。

**Architecture:** 修复全部落在**解析层**——表格在进入切分之前就被 `page.get_text("blocks")` 拍平了，事后补救无效。新增 `ingestion/tables.py` 负责「提取 → 表头继承 → 列名序列化 → 分组」，`pdf.py` 在本地路径接入并把表格组内联进页文本，`chunker.py` 增加「原子区间」参数保证组不被切开。

**Tech Stack:** PyMuPDF (`page.find_tables()`) / pytest / asyncpg

**Spec:** 本方案由 2026-10-15 的实测与调研直接得出，关键结论的出处：

| 结论 | 出处 |
|---|---|
| 每行重复表头 + 行级切分 = 检索最优 | [arXiv:2408.17008](https://arxiv.org/abs/2408.17008)（2⁴ 全因子实验，16 种表格表示 × 5 个 embedding） |
| 表头重复是主流框架默认行为 | Docling `HybridChunker.repeat_table_header=True`；Unstructured [PR #4298](https://github.com/Unstructured-IO/unstructured/pull/4298) |
| 表头必须**先扣 token 预算再切** | Docling [#3428](https://github.com/docling-project/docling/issues/3428)、LightRAG commit `0044de6` |
| **跨页合并会静默丢表** | RAGFlow [PR #18789](https://github.com/infiniflow/ragflow/pull/18789)（误并致输出 8 张表而非 9 张，第二张网格被丢） |
| 表头要进**纯文本字段**（BM25 也要） | Unstructured PR #4298（同时写 `chunk.text` 与 `text_as_html`） |
| **分组行的操作式定义** | PubTables-1M [arXiv:2110.00061](https://arxiv.org/abs/2110.00061) Algorithm 1 第 7 行：「不在列头里、恰好一个非空格子、且在第一列」 |
| **表头行与数据行的相邻行对比特征**（本项目只取了「纯数字占比」的简化版） | Fang et al., *Table Header Detection and Classification*, AAAI-12（[开放全文](https://clgiles.ist.psu.edu/pubs/AAAI2012-table-header.pdf)）；11 个单行特征 + 8 个相邻行对比特征，随机森林 F1 0.976，**不依赖线框与颜色** |
| **LLM 判表头不划算**（本项目不用 LLM 做表头判定） | VLDB 2025 TaDA Workshop, [Okhotin & Dorodnykh](https://www.vldb.org/2025/Workshops/VLDB-Workshops-2025/TaDA/TaDA25_14.pdf)：405B 模型 F1 82–84%，7–8B 仅 6–30%；且**表头不在第一行时性能最差**——恰好是本项目最需要它的场景 |

## Global Constraints

- **不改 OCR 路径。** MinerU 只服务扫描页；实测语料两份表格文档 `source='local'`、`ocr_pages=[]`，改造只碰 PyMuPDF 本地路径。
- **不做跨页表格合并。** 只做「表头继承」。每页表格保持独立对象，**一行都不丢**。判据不满足时降级为当前行为（无表头），不冒险。
- **偏移参照系不变。** `char_start/char_end` 仍是规范化文本的 Unicode 码点偏移；表格文本内联进 `PageText.text` 后由 `build_normalized_text` 重新拼装，偏移自洽。
- **`chunk_id` 生成规则不变**（`f"{document_id}:{chunk_index}"`），四处在用，不得改动。
- **序列化用键值行**（用户 2026-10-15 决定）：`列名：值｜列名：值`。分隔符用 `｜`(U+FF5C)，**不是** `_SENTENCE_END`/`_SECONDARY_END` 里的任何字符。
- **`squeeze` 放宽**（用户 2026-10-15 决定）：除空白外，额外剔除 `｜` 与 `：`。校验强度降低是已知并接受的代价。
- **破坏性变更**：本改动使 `normalized_text` 与旧版不同，**必须重跑入库**。语料中仅 2 份文档含表格（`11_` 8 个表、`12_` 5 个表）。

## Review Focus

以下七类是本改动最可能出问题、且没有任务测试覆盖的输入，逐条列出。每条对应的测试已写进它所属任务的步骤里。

1. **锚表贴页底、但本表不在页顶** —— 这是最容易误判的形状：只看「上一张表伸到了页面下部」就判定续页。读者会期望它是两张独立的表。doc 11 的 `p5→p6` 正是这个形状（锚表底 689、本表顶 166）。→ Task 2 Step 1（`test_doc11_anchor_near_bottom_is_not_enough`）
2. **表格没有任何一行是表头** —— 读者会期望程序照常输出数据行，而不是崩掉或吞掉首行。→ Task 1 Step 1
3. **单元格文本超过分组预算**（单个格子就比 chunk_size 还长）—— 读者会期望它自成一组且不被切开，而不是进入死循环。→ Task 3 Step 1
4. **表格组落在页边界上** —— 读者会期望表格组不被页边界从中间劈开，因为「不切断表格」优先于「chunk 不跨页」。→ Task 4 Step 1
5. **表格单元格里含 `｜` 或 `【表头】` 字样** —— 读者会期望序列化不被这些内容破坏，扫描器不误判。→ Task 3 Step 1（`test_escape_cell_separators`）
6. **表头行在多页重复且很短** —— 读者会期望它留下。它 35 字、每页重复，正好命中 `find_repeated_lines` 的「跨页重复短行 = 页眉」特征：8 页文档阈值 4，表头出现 4 次即被**整片删除**，改造对那份文档静默失效（doc 11 正是 8 页）。→ Task 5 Step 1（`test_table_group_marker_survives_cleaning`）
7. **同一张逻辑表跨页列数变化** —— 读者会期望它们仍被认作同一张表。doc 12 的 p8 是 6 列、p9–p12 是 7 列（多的那列「获奖等级」承载真实数据），「列数必须相等」的判据会把真续页判掉。→ Task 2 Step 1（`test_inherit_across_one_column_difference`）

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `backend/app/ingestion/tables.py` | 表格提取、表头继承、合并单元填充、键值序列化与分组 | **新建** |
| `backend/app/ingestion/chunker.py` | 增加 `atomic` 参数：这些区间各自成块、内部不切 | 修改 |
| `backend/app/ingestion/loaders/pdf.py` | 本地路径：提取表格 → 剔除表格块 → 按阅读顺序内联序列化文本 | 修改 |
| `backend/app/ingestion/pipeline.py` | 扫描 `【表头】` 标记算出原子区间，传给 `chunk_text` | 修改 |
| `backend/app/eval/generate.py` | `squeeze` 放宽 | 修改 |
| `backend/tests/unit/test_tables.py` | 表格模块单测 | 新建 |
| `backend/tests/unit/test_chunker_atomic.py` | 原子区间单测 | 新建 |
| `backend/tests/fixtures/eval_cases_v1.json` | 表格题的 `ground_truth` 改为新序列化形态 | 修改 |

---

## Task 1: 表格提取与网格归一

**Files:**
- Create: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Interfaces:**
- Consumes: 无（本任务是起点）
- Produces:
  - `@dataclass RawTable(bbox: tuple[float,float,float,float], headers: list[str], rows: list[list[str]], page: int)`
  - `normalize_grid(rows: list[list[str]]) -> list[list[str]]` —— 丢弃**全空列**、右侧补齐到等宽
  - `forward_fill(rows: list[list[str]], *, skip_cols: frozenset[int] = frozenset()) -> list[list[str]]`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_tables.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v`

Expected: FAIL，`ModuleNotFoundError: No module named 'app.ingestion.tables'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/ingestion/tables.py`：

```python
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


#: 序列化用的分隔符。**必须避开 `chunker._SENTENCE_END` / `_SECONDARY_END`**，
#: 否则切分器会在分隔符处断句，把表格组切碎 —— 那正是本次要修的病。
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
        for i, cell in enumerate(new):
            if i in skip_cols:
                last[i:i + 1] = [cell]
                continue
            if cell.strip():
                last[i:i + 1] = [cell]
            elif i < len(last) and last[i]:
                new[i] = last[i]
        out.append(new)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v`

Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/tables.py backend/tests/unit/test_tables.py
git commit -m "[feat] 表格模块：网格归一与合并单元格向下填充"
```

---

## Task 2: 表头继承（不做合并）

**Files:**
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Interfaces:**
- Consumes: Task 1 的 `RawTable`、`normalize_grid`
- Produces:
  - `extract_page_tables(page) -> list[RawTable]` —— 包一层 `page.find_tables()`
  - `inherit_headers(tables: list[RawTable], prev: RawTable | None, *, page_height: float) -> list[RawTable]` —— 返回处理后的表；无表头的表被赋上 `prev.headers`，其首行归为数据

**背景（实现者必读）**：`page.find_tables()` 的 `header.external` 在续页上**也是 `False`**（实测 doc 12 第 8–12 页全是 `False`），所以**不能靠它判断有无表头**。必须用下面的显式判据。

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/unit/test_tables.py`：

```python
from app.ingestion.tables import (
    RawTable, inherit_headers, is_continuation, is_projected_row_header,
)


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
        t = _t(pg, top, bot, [], [["x"] * nc] * 2)
        out = inherit_headers([t], prev, page_height=PH)[0]
        if i == 0:
            assert out.headers == hdr6, "首表从自己的首行取表头"
        else:
            assert out.headers == hdr6, f"p{pg} 应继承到表头"
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v -k inherit or header`

Expected: FAIL，`ImportError: cannot import name 'inherit_headers'`

- [ ] **Step 3: 写最小实现**

追加到 `backend/app/ingestion/tables.py`：

```python
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
```

顶部补 `import re`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v`

Expected: 21 passed

- [ ] **Step 5: 用真实文档验证继承行为**

Run:

```bash
cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -c "
import pymupdf
from app.ingestion.tables import extract_page_tables, inherit_headers
d = pymupdf.open('corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf')
prev = None
for i in range(7, 12):
    p = d.load_page(i)
    ts = extract_page_tables(p)
    ts = inherit_headers(ts, prev, page_height=p.rect.height)
    for t in ts:
        print(f'p{i+1}: 列数={len(t.rows[0])} 表头={t.headers[:2]} 行数={len(t.rows)}')
        prev = t
"
```

Expected: 第 8 页拿到真表头 `['课程类别','课程项目',...]`；第 9–12 页继承到同一表头，且行数不减少（8/20/25/21/17 附近）。**若某页显示 `表头=[]`，说明该页判据没过而降级 —— 这是允许的，但要记录在案。**

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/tables.py backend/tests/unit/test_tables.py
git commit -m "[feat] 表头继承：续页表补表头，不做跨页合并（RAGFlow #18789 教训）"
```

---

## Task 3: 键值序列化与分组

**Files:**
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Interfaces:**
- Consumes: Task 2 的 `RawTable`
- Produces: `serialize_groups(table: RawTable, *, budget: int) -> list[str]` —— 每组是一个字符串，首行为 `【表头】列名｜列名…`，其后每行为 `列名：值｜列名：值`

**依据**：[arXiv:2408.17008](https://arxiv.org/abs/2408.17008) 实验证明「表头重复进每个单元格 + 行级切分」检索最优。表头**必须先扣预算再分组** —— Docling [#3428](https://github.com/docling-project/docling/issues/3428) 正是没扣预算导致超限。

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/unit/test_tables.py`：

```python
from app.ingestion.tables import GROUP_MARK, serialize_groups
from app.ingestion.chunker import _SECONDARY_END, _SENTENCE_END


def _tbl(headers, rows):
    return RawTable(page=1, bbox=(0.0, 0.0, 100.0, 100.0), headers=list(headers), rows=rows)


def test_every_row_carries_its_column_names():
    """★核心断言：序列化后每一行都自带完整列名，切到哪都不丢归属。"""
    t = _tbl(["课程内容", "参考学分"], [["无偿献血", "2分/次"]])
    groups = serialize_groups(t, budget=500)
    assert groups[0].splitlines()[1] == "课程内容：无偿献血｜参考学分：2分/次"


def test_group_starts_with_header_line():
    t = _tbl(["参考学分"], [["2分/次"]])
    assert serialize_groups(t, budget=500)[0].splitlines()[0].startswith(GROUP_MARK)


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
    """★单元格里含分隔符或标记 —— 转义后不破坏结构，扫描器不误判。"""
    t = _tbl(["参考学分"], [["含｜竖线的值", "含【表头】标记的值"]])
    groups = serialize_groups(t, budget=500)
    body = groups[0].splitlines()[1]
    # 正文里不能再出现裸的 GROUP_MARK（否则被当成新组的开头）
    assert body.count(GROUP_MARK) == 0


def test_serialization_separators_never_break_a_chunk():
    """★分隔符必须避开切分器的断句字符，否则组会被切碎。"""
    assert SEP not in _SENTENCE_END
    assert SEP not in _SECONDARY_END


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
    body = serialize_groups(t, budget=500)[0].splitlines()
    assert len(body) == 3, "表头 + 2 条数据行；PRH 不该单独成行"
    assert body[1] == "课程类别：实践实习与社会工作｜课程内容：无偿献血｜参考学分：2分/次"
    assert body[2].startswith("课程类别：实践实习与社会工作")


def test_forward_fill_does_not_swallow_first_data_row():
    """填充不能把真实数据行吃掉 —— 首行有数据的行照常输出。"""
    t = _tbl(["课程类别", "参考学分"], [["甲类", "5分"], ["", "3分"]])
    body = serialize_groups(t, budget=500)[0].splitlines()
    assert len(body) == 3
    assert body[1] == "课程类别：甲类｜参考学分：5分"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v -k serialize or escape or oversized or deducts`

Expected: FAIL，`ImportError: cannot import name 'serialize_groups'`

- [ ] **Step 3: 写最小实现**

追加到 `backend/app/ingestion/tables.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v`

Expected: 30 passed

- [ ] **Step 5: 用真实表格验证膨胀与分组数**

Run:

```bash
cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -c "
import pymupdf
from app.ingestion.tables import extract_page_tables, inherit_headers, serialize_groups
d = pymupdf.open('corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf')
prev, allg = None, []
for i in range(7, 12):
    p = d.load_page(i)
    for t in inherit_headers(extract_page_tables(p), prev, page_height=p.rect.height):
        allg += serialize_groups(t, budget=500); prev = t
print('组数:', len(allg))
print('总字符:', sum(len(g) for g in allg))
print('超 500 的组:', [len(g) for g in allg if len(g) > 500])
grp = [g for g in allg if '无偿献血' in g]
print('含无偿献血的组:'); print(grp[0][:300] if grp else '未找到')
"
```

Expected: 组数 20–30；**超 500 的组为 0**；「无偿献血」那行完整带着 `课程内容：无偿献血｜参考学分：2分/次`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/tables.py backend/tests/unit/test_tables.py
git commit -m "[feat] 表格键值序列化：每行自带列名，表头先扣预算（arXiv:2408.17008）"
```

---

## Task 4: chunker 支持原子区间

**Files:**
- Modify: `backend/app/ingestion/chunker.py`
- Test: `backend/tests/unit/test_chunker_atomic.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `chunk_text(text, *, ..., atomic: list[tuple[int, int]] | None = None) -> list[ChunkSpan]`

**为什么必须加这个参数**：`_best_break` 是**从 `limit` 往回找**断句点，所以即使表格组短于 `chunk_size`，它也会在组内的 `；`/`：` 处提前断开。光靠 `boundaries` 挡不住。原子区间是唯一能**保证**组不被切开的机制。

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_chunker_atomic.py`：

```python
"""切分器的原子区间（§3.4.1 表格不被切断）。"""
from __future__ import annotations

from app.ingestion.chunker import chunk_text


def test_atomic_range_is_never_split():
    """★原子区间内部不切 —— 哪怕里面有断句标点、且区间超过 chunk_size。"""
    body = "甲；乙；丙；丁；戊；己；庚；辛；壬；癸；" * 8     # 大量；且远超 60 字
    text = "前文。\n\n" + body + "\n\n后文。"
    start = text.index(body)
    spans = chunk_text(text, chunk_size=60, overlap=10, min_size=5,
                       atomic=[(start, start + len(body))])
    holding = [s for s in spans if s.char_start <= start < s.char_end]
    assert len(holding) == 1, "原子区间必须完整落在同一个 chunk 里"
    assert body in holding[0].text


def test_atomic_range_is_not_merged_with_prose():
    """★原子区间不与相邻正文合并 —— 表格块要独立成块（Unstructured 的硬保证）。"""
    body = "甲；乙；丙"
    text = "前文很长很长很长很长很长很长很长很长。" + body + "后文很长很长很长很长很长很长很长。"
    start = text.index(body)
    spans = chunk_text(text, chunk_size=500, overlap=50, min_size=5,
                       atomic=[(start, start + len(body))])
    holder = [s for s in spans if s.char_start <= start < s.char_end][0]
    assert "前文" not in holder.text and "后文" not in holder.text


def test_atomic_overlapping_page_boundary_is_not_cut():
    """★表格组跨页边界时，原子优先 —— 「表格不切断」高于「chunk 不跨页」。"""
    body = "甲；乙；丙；丁"
    text = "第一页正文。" + body + "第二页正文。"
    b = text.index(body)
    page_boundary = b + 3                    # 边界落在组内部
    spans = chunk_text(text, chunk_size=500, overlap=50, min_size=5,
                       boundaries=[page_boundary], atomic=[(b, b + len(body))])
    holder = [s for s in spans if s.char_start <= b < s.char_end][0]
    assert body in holder.text


def test_atomic_none_behaves_as_before():
    """不传 atomic 时行为与改造前一致 —— 回归保护。"""
    text = "第一句。第二句。第三句。"
    assert len(chunk_text(text, chunk_size=10, overlap=2, min_size=2)) == len(
        chunk_text(text, chunk_size=10, overlap=2, min_size=2, atomic=None))


def test_atomic_ranges_are_sorted_and_clamped():
    """乱序/越界的区间不炸 —— 调用方传错不该让整篇入库失败。"""
    text = "甲乙丙丁戊己庚辛。"
    spans = chunk_text(text, chunk_size=4, overlap=1, min_size=2,
                       atomic=[(99, 200), (2, 4), (0, 1)])
    assert "".join(s.text for s in spans).startswith("甲")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_chunker_atomic.py -v`

Expected: FAIL，`TypeError: chunk_text() got an unexpected keyword argument 'atomic'`

- [ ] **Step 3: 写最小实现**

修改 `backend/app/ingestion/chunker.py`。把 `chunk_text` 的签名和主循环改掉：

```python
def chunk_text(
    text: str,
    *,
    chunk_size: int | None = None,
    overlap: int | None = None,
    min_size: int | None = None,
    boundaries: list[int] | None = None,
    base_offset: int = 0,
    atomic: list[tuple[int, int]] | None = None,
) -> list[ChunkSpan]:
    """把文本切成 chunk。

    `boundaries` 是一组**不允许跨越**的字符偏移（如 PPTX 的幻灯片起始位置）。
    切分遇到下一个 boundary 就收尾，即使还没到 chunk_size。
    `base_offset` 加到 char_start/char_end 上，用于分页拼装后仍指向全局规范化文本。

    `atomic` 是一组**不允许切开**的区间 `[(start, end), ...]`（表格组）。
    ⚠️ 光有 `boundaries` 挡不住表格被切 —— `_best_break` 是**从 limit 往回找**
       断句点，所以组内只要有 `；`/`：` 就会提前断开。原子区间是唯一的保证。
       区间**优先于 boundary**：表格不切断 > chunk 不跨页。
    """
    chunk_size = chunk_size or int(cfg("chunking.chunk_size", 500))
    overlap = overlap if overlap is not None else int(cfg("chunking.chunk_overlap", 50))
    min_size = min_size or int(cfg("chunking.chunk_min_size", 5))

    text = text or ""
    if not text.strip():
        return []

    hard_stops = sorted(b for b in (boundaries or []) if 0 < b < len(text))

    # 原子区间：裁到文本范围内、丢掉无效的、按起点排序
    spans_atomic = sorted(
        (s, e) for s, e in (atomic or []) if 0 <= s < e <= len(text)
    )

    chunks: list[ChunkSpan] = []
    index = 0
    pos = 0
    total = len(text)

    def _emit(start: int, end: int) -> None:
        nonlocal index
        piece = text[start:end]
        if piece.strip():
            chunks.append(ChunkSpan(
                chunk_index=index,
                char_start=base_offset + start,
                char_end=base_offset + end,
                text=piece.strip(),
            ))
            index += 1

    while pos < total:
        # ---- ① 落在原子区间起点：整段吐出，不切、不重叠 ----
        hit = next(((s, e) for s, e in spans_atomic if s <= pos < e), None)
        if hit is not None:
            _emit(hit[0], hit[1])
            pos = hit[1]
            continue

        # ---- ② 普通文本：先按 boundary 收窄 ----
        hard_limit = total
        for b in hard_stops:
            if b > pos:
                hard_limit = min(hard_limit, b)
                break
        # 下一个原子区间起点也是硬上界 —— 正文不该吃进表格
        for s, _e in spans_atomic:
            if s > pos:
                hard_limit = min(hard_limit, s)
                break

        limit = min(pos + chunk_size, hard_limit)
        floor = min(pos + max(min_size, chunk_size // 4), limit)
        end = _best_break(text, pos, limit, floor=floor)
        _emit(pos, end)

        if end >= total:
            break
        if end >= hard_limit:
            pos = end
            continue
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = end
        pos = next_pos

    return chunks
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_chunker_atomic.py -v`

Expected: 5 passed

- [ ] **Step 5: 跑既有测试确认没回归**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/ -v`

Expected: 全绿（既有单测不受影响）

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/chunker.py backend/tests/unit/test_chunker_atomic.py
git commit -m "[feat] chunker 原子区间：表格组不被切断，优先级高于页边界"
```

---

## Task 5: pdf.py 接入表格提取

**Files:**
- Modify: `backend/app/ingestion/loaders/pdf.py`
- Test: `backend/tests/integration/test_loaders.py`（追加用例）

**Interfaces:**
- Consumes: Task 1–3 的 `extract_page_tables` / `inherit_headers` / `serialize_groups` / `GROUP_MARK`
- Produces: `PageText.text` 里的表格区域被替换为序列化文本；`_text_blocks` 新增返回值

**关键实现点**：表格的**原始拍平文本必须先从 block 文本里剔除**，否则表格内容会出现两遍（一遍拍平、一遍序列化）。

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/integration/test_loaders.py`：

```python
def test_pdf_table_is_serialized_with_column_names():
    """★表格在 PageText.text 里是键值行，不再是拍平的单格行。"""
    from pathlib import Path
    from app.ingestion.loaders.pdf import load_pdf
    from app.ingestion.tables import GROUP_MARK

    pdf = Path("corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf")
    if not pdf.exists():
        pytest.skip("语料不在仓库中")
    res = load_pdf(pdf)
    merged = "\n".join(p.text for p in res.pages)
    assert GROUP_MARK in merged, "表头行必须出现"
    assert "课程内容：无偿献血" in merged, "数据行必须自带列名"


def test_pdf_table_text_appears_only_once():
    """★表格内容不能出现两遍（拍平版 + 序列化版）。"""
    from pathlib import Path
    from app.ingestion.loaders.pdf import load_pdf

    pdf = Path("corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf")
    if not pdf.exists():
        pytest.skip("语料不在仓库中")
    res = load_pdf(pdf)
    merged = "\n".join(p.text for p in res.pages)
    assert merged.count("捐献造血干细胞") == 1, "同一个单元格出现了多次"


def test_table_group_marker_survives_cleaning():
    """★表头行不能被「跨页重复行」规则当成页眉删掉。

    实测：表头行 35 字 ≤ `find_repeated_lines` 的 `max_len=40`，
    而它每页都重复。8 页文档的阈值是 `int(8*0.6)=4`，
    表头在 4 页出现 → **恰好达到阈值会被整片删除**（doc 11 正是 8 页）。
    结果就是改造对那份文档完全失效，且不报任何错。
    """
    from app.ingestion.clean import build_normalized_text
    from app.ingestion.tables import GROUP_MARK

    hdr = GROUP_MARK + "课程类别｜课程项目｜课程内容｜参考学分｜开课单位｜获得学分方式"
    pages = [(i + 1, f"第{i + 1}页正文。\n{hdr}\n课程类别：甲｜参考学分：5分") for i in range(8)]
    normalized, _ = build_normalized_text(pages)
    assert normalized.count(GROUP_MARK) == 8, "每个表头行都必须留下"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_loaders.py -v -k table`

Expected: FAIL —— `test_pdf_table_is_serialized_with_column_names` 报 `assert GROUP_MARK in merged`；`test_table_group_marker_survives_cleaning` 报 `assert 0 == 8`（表头行全被删了）

- [ ] **Step 3: 写最小实现**

**改 ①：`clean.py` 让表头行免疫删除。** 在 `clean_page` 的循环里，把 `repeated` 判断**之前**加一条：

```python
        if stripped.startswith(GROUP_MARK):
            # 表格组标记永不删 —— 它每页重复且很短，正好命中「页眉页脚」特征。
            # 删掉就等于把整个表格改造静默作废（见 clean_page 的测试）。
            kept.append(_MULTI_SPACE_RE.sub(" ", line))
            continue
        if stripped in repeated:
            continue
```

文件顶部补 `from app.ingestion.tables import GROUP_MARK`。

**改 ②：`_text_blocks` 返回块列表。** 现在返回的是 join 好的字符串，**没法与 bbox 对齐**——每个块的文字自带换行（实测 doc 12 第 8 页 61 个块，join 后按 `\n` 分割变成 175 段）。表格剔除依赖这个对齐。

```python
def _text_blocks(page: "fitz.Page") -> tuple[list[str], list[dict]]:
    """取逐块文字与 bbox。

    ⚠️ 返回**块列表**，不是 join 好的字符串：块的文字自带换行，
       拿 join 后的串去和 bbox 一一对齐会严重错位（61 块 → 175 段）。
       表格剔除按 `zip(blocks, boxes)` 逐块判断，依赖这个一一对应。
    """
    blocks = page.get_text("blocks")
    parts: list[str] = []
    boxes: list[dict] = []
    for block in blocks:
        x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        if not text or not text.strip():
            continue
        parts.append(text)
        boxes.append({"x0": round(x0, 1), "x1": round(x1, 1),
                      "top": round(y0, 1), "bottom": round(y1, 1)})
    return parts, boxes
```

**改 ③：新增 `_text_with_tables`。**

```python
def _text_with_tables(blocks: list[str], boxes: list[dict], page: "fitz.Page",
                      *, prev_table=None) -> tuple[str, list[dict], object]:
    """把表格区域从拍平文本里换成键值序列化文本。

    ⚠️ 表格的原始拍平文本**必须剔除**，否则表格内容会出现两遍
       （一遍 get_text("blocks") 的拍平版，一遍序列化版）。
       剔除依据是 bbox 包含 —— 落进表格 bbox 的块就属于该表。

    返回 (新文本, 剩余 boxes, 本页最后一张表)。`blocks` 与 `boxes` 必须一一对应。
    """
    from app.ingestion.tables import (
        extract_page_tables, inherit_headers, serialize_groups,
    )
    from app.ingestion.chunker import chunk_size_hint

    tables = extract_page_tables(page)
    if not tables:
        return "\n".join(blocks), boxes, prev_table

    tables = inherit_headers(tables, prev_table, page_height=page.rect.height)
    table_boxes = [t.bbox for t in tables if t.rows]
    if not table_boxes:
        return "\n".join(blocks), boxes, prev_table

    # ① 剔除落进表格 bbox 的块（2pt 容差）
    kept_blocks: list[str] = []
    kept_boxes: list[dict] = []
    for blk, box in zip(blocks, boxes):
        inside = any(
            b[0] - 2 <= box["x0"] and box["x1"] <= b[2] + 2
            and b[1] - 2 <= box["top"] and box["bottom"] <= b[3] + 2
            for b in table_boxes
        )
        if not inside:
            kept_blocks.append(blk)
            kept_boxes.append(box)

    # ② 序列化每张表，按 bbox 上沿 y 插入到阅读顺序里
    pieces: list[tuple[float, str]] = []
    for t in tables:
        for g in serialize_groups(t, budget=chunk_size_hint()):
            pieces.append((t.bbox[1], g))

    out: list[str] = []
    cursor = 0
    for top, group in sorted(pieces, key=lambda x: x[0]):
        while cursor < len(kept_boxes) and kept_boxes[cursor]["top"] <= top:
            out.append(kept_blocks[cursor])
            cursor += 1
        out.append("")              # 组前留空行 —— table_atomic_ranges 靠空行切段
        out.append(group)
    out.append("")
    out.extend(kept_blocks[cursor:])

    return "\n".join(out), kept_boxes, (tables[-1] if tables else prev_table)
```

**改 ④：`chunker.py` 补一个读配置的小函数**（别让 pdf.py 直接 import 私有常量）：

```python
def chunk_size_hint() -> int:
    """表格分组的预算 —— 与 chunk_size 同源，避免两处各写一个数。"""
    return int(cfg("chunking.chunk_size", 500))
```

**改 ⑤：接进 `load_pdf` 的本地分支**（原第 407–430 行附近）。

`extracted` 的元素要改成**保留块列表**（原来存的是 join 后的文本，块列表丢了就没法按 bbox 剔除表格块）：

```python
        extracted: list[tuple[int, "fitz.Page", list[str], list[dict]]] = []
        for index in range(total):
            page = doc.load_page(index)
            block_texts, boxes = _text_blocks(page)
            extracted.append((index + 1, page, block_texts, boxes))   # 1-based 绝对页码

        # 整份级初判（§E.8.1）：整份像扫描件时，所有页都走 OCR。
        # 注意它是**整份**判据，不能拿它逐页判。
        whole_scanned = document_looks_scanned(
            doc, ["\n".join(bt) for _, _, bt, _ in extracted])
```

本地分支里直接拿块列表：

```python
        prev_table = None
        for page_no, page, block_texts, boxes in extracted:
            text = "\n".join(block_texts)       # 竖排判定与可用性判定都用 join 后的文本
            if (not whole_scanned) and _page_is_usable(page, text):
                # 竖排检测必须在清洗之前（E.3.3）
                if single_char_line_ratio(text) >= float(
                        cfg("ingestion.vertical_text_single_char_ratio", 0.5)):
                    text = reverse_line_order(text)

                # 表格：结构化后替换拍平文本（OCR 路径不碰）
                text, boxes, prev_table = _text_with_tables(
                    page, block_texts, boxes, prev_table=prev_table)

                result.pages.append(PageText(
                    page=page_no,
                    text=text,
                    bbox=[{"page": page_no, **b} for b in boxes],
                    image_paths=_extract_images(doc, page, page_no, image_dir)
                                  if image_dir else [],
                ))
            else:
                prev_table = None          # 换页路径要断掉继承链
                needs_ocr.append(page_no)
```

> **注意竖排页**：`reverse_line_order(text)` 只作用于 join 后的整体文本，而 `_text_with_tables` 拿的是**原始块列表**。竖排页上的表格会因此拿不到反转后的文本。这是已知限制——实测两份表格文档的表格页都不是竖排页（`single_char_line_ratio` 未触发）。若将来遇到竖排+表格的文档，需要先把块列表也反转。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_loaders.py -v -k table`

Expected: 3 passed

- [ ] **Step 5: 跑全量加载器测试确认没回归**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_loaders.py backend/tests/integration/test_pdf_samples.py backend/tests/unit/ -v`

Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/loaders/pdf.py backend/app/ingestion/clean.py backend/app/ingestion/chunker.py backend/tests/integration/test_loaders.py
git commit -m "[feat] PDF 本地路径接入表格结构化：剔除拍平文本，内联键值序列化"
```

---

## Task 6: pipeline 传递原子区间

**Files:**
- Modify: `backend/app/ingestion/pipeline.py`
- Test: `backend/tests/integration/test_pipeline.py`（追加用例）

**Interfaces:**
- Consumes: Task 4 的 `chunk_text(..., atomic=)`、Task 3 的 `GROUP_MARK`
- Produces: `table_atomic_ranges(normalized: str) -> list[tuple[int, int]]`（新增辅助函数，放在 `tables.py`）

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/unit/test_tables.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v -k atomic_ranges`

Expected: FAIL，`ImportError: cannot import name 'table_atomic_ranges'`

- [ ] **Step 3: 写最小实现**

追加到 `backend/app/ingestion/tables.py`：

```python
def table_atomic_ranges(normalized: str) -> list[tuple[int, int]]:
    """扫描规范化文本，返回每个表格组的字符区间。

    组 = 以 `GROUP_MARK` 开头、到**下一个空行**为止的块。
    `serialize_groups` 保证组内不出现空行、组间恰好空一行，
    所以「到下一个空行」等价于「到本组结束」。
    """
    out: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = normalized.find(GROUP_MARK, pos)
        if start == -1:
            break
        # 回到行首（GROUP_MARK 前可能缩进）
        line_start = normalized.rfind("\n", 0, start) + 1
        # 找下一个空行
        blank = normalized.find("\n\n", start)
        end = blank if blank != -1 else len(normalized)
        out.append((line_start, end))
        pos = end
    return out
```

修改 `backend/app/ingestion/pipeline.py`（原第 209 行附近）：

```python
        from app.ingestion.tables import table_atomic_ranges

        spans: list[ChunkSpan] = chunk_text(
            normalized,
            boundaries=boundaries,
            atomic=table_atomic_ranges(normalized),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_tables.py -v -k atomic_ranges`

Expected: 3 passed

- [ ] **Step 5: 端到端验证：重新解析 doc 12，确认表格组不被切开**

Run:

```bash
cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -c "
from pathlib import Path
from app.ingestion.loaders.pdf import load_pdf
from app.ingestion.clean import build_normalized_text
from app.ingestion.chunker import chunk_text
from app.ingestion.tables import table_atomic_ranges, GROUP_MARK

res = load_pdf(Path('corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf'))
norm, slices = build_normalized_text([(p.page, p.text) for p in res.pages])
spans = chunk_text(norm, boundaries=[s.char_start for s in slices[1:]],
                   atomic=table_atomic_ranges(norm))
groups = [s for s in spans if s.text.startswith(GROUP_MARK)]
print('总 chunk:', len(spans), ' 表格组 chunk:', len(groups))
broken = [s.chunk_index for s in spans
          if GROUP_MARK in s.text and not s.text.startswith(GROUP_MARK)]
print('★被切碎的表格 chunk:', broken or '无')
# 抽查：无偿献血行是否与 '2分/次' 同块
hit = [s for s in spans if '无偿献血' in s.text]
print('无偿献血所在块含2分/次:', all('2分/次' in s.text for s in hit))
"
```

Expected: `被切碎的表格 chunk: 无`；`无偿献血所在块含2分/次: True`

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/tables.py backend/app/ingestion/pipeline.py
git commit -m "[feat] pipeline 传递表格原子区间：表格组在切分中保持完整"
```

---

## Task 7: 放宽 squeeze 并改写表格题 ground_truth

**Files:**
- Modify: `backend/app/eval/generate.py`
- Modify: `backend/tests/fixtures/eval_cases_v1.json`（表格题）
- Modify: `backend/eval_sets/第二课堂评分表专项.json`（Task 8 复测用）
- Test: `backend/tests/unit/test_eval_fixture.py`

**Interfaces:**
- Consumes: 无
- Produces: `squeeze` 新行为 —— 除空白外，额外剔除 `｜` 与 `：`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/unit/test_eval_fixture.py`：

```python
def test_squeeze_strips_table_serialization_separators():
    """★键值行的分隔符不算「字」—— 否则表格题的标准答案永远校验不过。"""
    from app.eval.generate import squeeze

    assert squeeze("课程内容：无偿献血｜参考学分：2分/次") == "课程内容无偿献血参考学分2分/次"
    assert squeeze("无偿献血\n2分/次") == "无偿献血2分/次"


def test_squeeze_still_strips_whitespace():
    """原有行为不回归。"""
    from app.eval.generate import squeeze

    assert squeeze(" 甲 \n\n 乙 ") == "甲乙"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_eval_fixture.py -v -k squeeze`

Expected: FAIL，`assert '课程内容：无偿献血...' == '课程内容无偿献血...'`

- [ ] **Step 3: 写最小实现**

修改 `backend/app/eval/generate.py` 的 `squeeze`：

```python
def squeeze(s: str) -> str:
    """去掉**所有空白 + 表格序列化分隔符**再比。用于「逐字」校验。

    ⚠️ 空白必须去掉：规范化正文里有 PDF 提取留下的硬换行与空行
    （实测：`…提出申请并经\\n\\n学院审核同意后送达；`），模型复述时自然写成
    一行 —— 用带空白的原串去比会判成「不是原文」，而**它确实是原文**。

    ⚠️ `｜` 与 `：` 也要去掉（2026-10-15 放宽）：表格改成键值序列化后
    （`参考学分：2分/次`），标准答案与正文之间的分隔符不属于「字」。
    代价如实写明：**含 `：` 的正文题校验强度会降低** —— 去掉冒号后，
    原本隔着冒号的两段文字会被判成连续。这是负责人 2026-10-15 明确接受的取舍。

    `tools/make_eval_cases.py` 与校验脚本都引用这里，不另抄一份。
    """
    return re.sub(r"[\s｜：]+", "", s or "")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_eval_fixture.py -v -k squeeze`

Expected: 2 passed

- [ ] **Step 5: 改写表格题 ground_truth**

表格题的 `ground_truth` 必须改成**与重新入库后的正文一致**的形态。对 `backend/eval_sets/第二课堂评分表专项.json` 的 10 条，按下表把 `ground_truth` 逐个替换（`question` 不动）：

| id | 新 ground_truth |
|---|---|
| sc-01 | `课程内容：无偿献血｜参考学分：2分/次` |
| sc-02 | `课程内容：捐献造血干细胞｜参考学分：10分/次` |
| sc-03 | `课程内容：大学英语六级｜参考学分：5分/项` |
| sc-04 | `课程内容：通过各级计算机等级考试并获得证书｜参考学分：3分/项` |
| sc-05 | `课程内容：校级学生组织负责人、团委兼职副书记｜参考学分：10分/学年` |
| sc-06 | `课程内容：获国家级奖项一、二、三等奖｜参考学分：18、15、12分/项` |
| sc-07 | `课程内容：特等奖（金奖）、一、二、三等奖｜参考学分：30、20、15、10分/项` |
| sc-08 | `课程内容：国家级立项｜参考学分：15分/项` |
| sc-09 | `课程内容：团支部书记、班长｜参考学分：3分/学年` |
| sc-10 | `要求学生每学年获得“思想政治与道德修养”类学分不少于15分`（正文题，不变） |

⚠️ **上表的字符串以 Task 8 重新入库后的实际正文为准**。若某条对不上，以正文实际内容为准改 ground_truth，**不要改序列化代码去迁就题目**。

- [ ] **Step 6: 提交**

```bash
git add backend/app/eval/generate.py backend/tests/unit/test_eval_fixture.py backend/eval_sets/第二课堂评分表专项.json
git commit -m "[fix] 放宽 squeeze 去表格分隔符；表格题 ground_truth 改为键值行形态"
```

---

## Task 8: 重跑入库并复测

**Files:**
- 无代码改动（数据操作 + 验证）

**Interfaces:**
- Consumes: Task 1–7 的全部产出
- Produces: 复测结论（写入 `corpus/guet/README.md`）

- [ ] **Step 1: 停掉后端（Chroma 是嵌入式的，同一索引不能两进程写）**

Run: `netstat -ano | grep :8090` 找到 PID，然后结束它。

Expected: `:8090` 不再监听

- [ ] **Step 2: 重新入库两份含表格的文档**

用「--infra-only 起容器 → 后端上传」或直接调 CLI 重跑摄入。**先删旧索引再传**，避免新旧混在一起：

```bash
cd /d/Knowledge_rag_system/backend
.venv/Scripts/python.exe -c "
import asyncio, sys
sys.path.insert(0, '.')
from app.ingestion import index_service
from app import db

async def main():
    await db.init_pool()
    for doc_id in ['d513cdbe13bf4de280f1b9542d7194f0']:   # 12_第二课堂
        await index_service.remove_from_index(doc_id)
        print('已移除索引', doc_id)
    await db.close_pool()

asyncio.run(main())
"
```

然后用管理端重新上传 `corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf`（`11_` 同理）。

Expected: 两份文档 `status='active'`，`chunk_count` 比改造前增加（表格条目变多）

- [ ] **Step 3: 验证入库后的正文形态**

Run:

```bash
cd /d/Knowledge_rag_system && .venv/Scripts/python.exe -c "
import asyncio, sys
sys.path.insert(0, 'backend')
from app import db

async def main():
    await db.init_pool()
    async with db.tx() as conn:
        rows = await conn.fetch(
            \"SELECT title, normalized_text_path FROM documents \"
            \"WHERE status='active' AND title LIKE '12_%'\")
    for r in rows:
        t = open(r['normalized_text_path'], encoding='utf-8').read()
        print(r['title'], '| 【表头】出现', t.count('【表头】'), '次')
        i = t.find('无偿献血')
        print('  上下文:', repr(t[max(0,i-60):i+30]))
    await db.close_pool()

asyncio.run(main())
"
```

Expected: 出现 `【表头】` 多次；`无偿献血` 前后是 `课程内容：…｜参考学分：2分/次`

- [ ] **Step 4: 重跑评测集，与改造前对比**

重新导入评测集并跑一轮（**注意：跑完把临时账号删掉**——建临时 admin 的做法见 2026-10-15 会话记录）：

```bash
cd /d/Knowledge_rag_system/backend
.venv/Scripts/python.exe tools/eval_set_io.py import "eval_sets/第二课堂评分表专项.json"
```

然后起后端 → 触发 run → 记录逐题指标。

Expected: 与改造前（`run_id=78433790…`）逐项对比。**重点看三件事**：
1. 拒答数（改造前 3/10）
2. `context_precision`（改造前 0.6583）
3. 那 3 道拒答题是否恢复作答

- [ ] **Step 5: 把结论写进语料 README**

在 `corpus/guet/README.md` 追加一节「表格文档的切分处理」，记录：改造前后的指标对比、`【表头】` 标记的约定、哪些行的表头是继承来的（降级过的）。**若某页降级未继承，必须写明** —— 否则后来人看到无表头的行会以为是 bug。

- [ ] **Step 6: 提交**

```bash
git add corpus/guet/README.md
git commit -m "[docs] 表格切分改造的前后指标对比与降级记录"
```

---

## 自检结果

**Spec 覆盖**：开工前确认的 6 项要求 → Task 1–3（提取/继承/序列化）、Task 4–6（不切断）、Task 7（评测契约）、Task 8（重跑验证）。无遗漏。

**Review Focus 覆盖**：7 条分别落在 Task 2 Step 1（锚表贴底但本表不在页顶、跨页列数变化）、Task 1 Step 1（无表头行）、Task 3 Step 1（超预算单元格、分隔符转义、分组行）、Task 4 Step 1（跨页边界）、Task 5 Step 1（表头行被当页眉删除）。全部有对应测试。

**自检中修掉的五处缺陷**（写方案与实测过程中发现，已改）：

1. `zip(text.split("\n"), boxes)` 会错位——块的文字自带换行，实测 61 块 join 后按 `\n` 分割成 175 段。改为 `_text_blocks` 返回块列表。
2. 原设计没有任何路径把「表格首行」采纳为表头——文档第一张表会退化成 `列1/列2/...`。已把「采纳」并入判据链，并把继承排在采纳之前。
3. **绝对 pt 阈值是错的**——实测页面有 62pt 上边距，表格不贴页顶/页底，锚表距页底 72/75/83/126pt。写死的 24pt 一处都通不过。改用**页高比例**（锚底 > 80%、本表顶 < 10%）。
4. **「列数必须相等」是错的**——实测 doc 12 的 p8 是 6 列、p9–p12 是 7 列，多的那列是「获奖等级」**承载真实数据**。第一版误以为它是空列。改用「列数差 ≤ 1」。
5. **`forward_fill` 定义了却没人调用**；且它必须在**分组行判定之前**做（PRH 行一经填充就变满格，认不出来）。已把它挪进 `serialize_groups`，并让 PRH 行不产生数据行。

**实测验证（判据改完后重跑）**：

| 语料 | 形状 | 结果 |
|---|---|---|
| doc 12 p8→p9/p9→p10/p10→p11/p11→p12 | 4 处真续页 | **4/4 通过** |
| doc 11 的 3 对跨页表 | 各自独立的表 | **3/3 正确拒绝** |

**调研得到的旁证（本项目未采纳，但记录在案）**：

- **不用 LLM 判表头**：VLDB 2025 实测 405B 才到 82–84% F1，7–8B 仅 6–30%，且「表头不在第一行」时最差——正是本项目的场景。MinerU 的做法可抄：LLM 功能默认全关，规则先行。
- **PyMuPDF 1.27.2.3 的 `_get_header`** 用加粗/背景色/文本属性/行距，实测在本语料**全部失效**（表头与数据行字体、字号、颜色、背景色完全一致）。
- **`Table.to_markdown(fill_empty=True)`** 是 PyMuPDF 自带的合并单元格填充（先按行、再按列复制），但没有跨页处理，表头仍默认取第 0 行——所以本项目仍需自己写表头继承。
- 新版 PyMuPDF 疑似有 `cell_type()` / body-window 的行类型判定，**本机 1.27.2.3 没有**，未评估。

**类型一致性**：`RawTable` / `inherit_headers` / `serialize_groups` / `table_atomic_ranges` / `GROUP_MARK` / `SEP` / `KV` 在 Task 1–7 间签名一致；`chunk_text(..., atomic=)` 在 Task 4 定义、Task 6 调用，参数名一致。

**已知未决**：Task 8 Step 2 的「重跑入库」具体入口（CLI 还是管理端上传）取决于当时后端状态，实施时以 `scripts/start.sh` 与管理端为准。
