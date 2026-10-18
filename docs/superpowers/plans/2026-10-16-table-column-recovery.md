# 表格列义恢复 + 自然句序列化 — 实施计划（方案乙）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用列坐标把跨页漂移表的表头恢复到正确的列上，并把所有表格统一渲染成自然句；表格组仍保持原子切分。

**Architecture:** `RawTable` 增加 `col_spans`（PyMuPDF 单元格的真实 x 区间）和 `col_headers`（按列对齐后的表头，`None` 表示新增的无名列）。续页表用“左边界优先 + 动态规划”和上一页表头对齐，而不是靠内容猜左邻。`table_render.py` 负责自然句渲染：doc 12 用专用模板，其他表格用通用“列名是值”的句子。`serialize_groups` 不再输出 `列名：值｜...` 键值行；`【表头】` 退化成**裸哨兵**，不再重复列名，`table_atomic_ranges` 和 `clean.py` 不用改。

**Tech Stack:** Python 3.13、PyMuPDF (`fitz`)、pytest；自然句渲染不引入新依赖。

**Spec:** 本文档 §1–§4（设计与实测）。相关方案：`2026-10-16-multi-value-expansion.md`（方案 1）、`2026-10-16-table-form-exclusion-and-units.md`（方案 3）。三份方案共享同一条渲染管线；本方案先落地。

---

## Global Constraints

- 当前分支：`refactor/campus-rag`。不直接提交 `main`。
- 语料基线（当前工作区版本）：
  - doc 12：`corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf`，290574 字节，md5 `5dc147e1d772b32bdae534ec3dc98279`。
  - doc 11：`corpus/guet/11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf`，409721 字节，md5 `d4382dff21353cac39112285452bdef8`。这是当前工作区版本；开工前先提交或至少把这个 md5 写进验收记录。
- **渲染统一自然句，不提供“键值格式 / 自然句”的配置开关。** 旧的 `｜` 键值行不再输出。
- **哨兵策略：** `GROUP_MARK = "【表头】"` 保留，但只作为裸标记；后面不再跟列名。`table_atomic_ranges`、`clean.py` 的豁免规则和 `_escape` 的 `【】` 转义都不改。
- 只改 PDF 本地文字层路径；MinerU/OCR 路径不动。
- 真实管线 `chunk_text` 必须带 `boundaries`（按页）和 `atomic`（表格组）；不要用“不按页切分”的离线数字做门禁。
- 当前默认精排后端是 Jina（`min_score=0.08`）；bge 的阈值是 `0.3`。门禁用 **rank-based 指标 + 当前后端阈值**，不要把 0.3 写死。
- 先做离线 A/B，不直接重建索引；重建索引需单独确认。
- 提交信息格式：`[类型] 简述`；只提交本方案相关文件。

---

## Review Focus

- **漂移表：** 7 列 / 6 个表头，多出来的“获奖等级”列在**中间**。绝不能把“课程内容”配到“获奖等级”，也不能把“参考学分”配到“开课单位”。
- **合并单元格：** `forward_fill` 后的值只用于展示合并列（类别/项目/单位/方式）；无名列必须用原始行，否则 p9 的“创业实体入驻…”会被填成上一行的“一、二、三等奖”，p10 会被填成“其他”。
- **对齐表：** doc 11 的表头数等于列数，不能被新逻辑降级成 `列N`。
- **通用表：** 没有 doc 12 表头时，自然句仍要保留列名和值，不能丢列。
- **原子区间：** 裸哨兵仍要能被 `table_atomic_ranges` 找到；表格组不能被切碎。
- **评测：** Jina 和 bge 分数刻度不同；目标块 rerank 排名和端到端拒答才是门禁。

---

## 1. 为什么改成几何列对齐

### 1.1 实测列坐标（doc 12，当前工作区 PDF）

PyMuPDF `Tab.cells` 的 x 边界取并集后，恰好得到 `col_count + 1` 个边界。实测：

| 页 | 列数 | x 边界（pt） | 对应列 |
|---|---|---|---|
| p8 | 6 | 30.7, 66.4, 158.5, 312.0, 365.1, 439.5, 564.5 | 类别 / 项目 / 内容 / 参考学分 / 开课单位 / 方式 |
| p9 | 7 | 30.7, 66.4, 158.5, 225.3, 313.0, 371.1, 442.3, 564.5 | 类别 / 项目 / 内容 / **获奖等级** / 参考学分 / 开课单位 / 方式 |
| p10 | 7 | 30.6, 66.4, 158.5, 227.0, 307.3, 371.1, 442.3, 564.5 | 同上 |
| p11 | 7 | 30.7, 66.3, 158.4, 221.8, 307.3, 375.9, 442.3, 564.5 | 同上 |
| p12 | 7 | 30.6, 66.3, 158.4, 215.2, 307.2, 371.0, 442.2, 564.4 | 类别 / 项目 / 内容 / **具体项目或等级** / 参考学分 / 开课单位 / 方式 |

关键观察：

- p8 的“内容”列左边界是 **158.5**；p9–p12 的“内容”列左边界仍是 **158.5**，新增的“获奖等级”列从 **225.3 / 227.0 / 221.8** 开始。
- p8 的“参考学分”左边界 312.0，对应 p9–p12 的 **313.0 / 307.3 / 307.3 / 307.3**；差值最大 4.7pt。
- p8 的“开课单位”左边界 365.1，对应 p9–p12 的 **371.1 / 371.1 / 375.9 / 371.1**；差值最大 10.8pt。
- p8 的“方式”左边界 439.5，对应 p9–p12 的 **442.3**；差值 2.8pt。

因此用**左边界 + 15pt 容差**就能把 6 个表头对齐到 7 列中的 6 列，剩下的第 4 列自然成为“未命名列”（获奖等级）。这比原方案的内容锚定 + 左锚补齐可靠，也不会被 p9/p12 的“方式列也含部门词”干扰。

### 1.2 对齐算法

输入：上一页表 `prev` 有 `col_spans`（长度 = 列数）和 `headers`（长度 = 列数）；当前表 `cur` 有 `col_spans`。

用动态规划做**保序对齐**，允许跳过表头或跳过列：

- 匹配代价 = `abs(prev_left - cur_left)`，超过 `X_TOL=15.0` 不允许匹配。
- 跳过表头代价 `UNMATCHED_PENALTY=1000.0`（远大于任何有效匹配）。
- 回溯得到 `col_headers: list[str | None]`，长度 = cur 列数。

对齐后再做一次**内容校验**：

- 映射到“参考学分”的列，≥90% 非空值含 `数字+分`；
- 映射到“开课单位”的列，≥80% 非空值含 `学院|团委|中心|党委|部（处）|教务处|处`。
- 校验失败 → 返回空映射，走位置标签回退，绝不硬贴。

### 1.3 目标 `col_headers`

doc 12：

```
p8:  ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
p9-p12: ["课程类别", "课程项目", "课程内容", null, "参考学分", "开课单位", "获得学分方式"]
```

doc 11 的表头数等于列数，直接使用自己的表头，不走几何对齐。

### 1.4 自然句目标格式

doc 12（有未命名列，非多值行）：

```
技能特长 - 语言类证书 - 大学英语及专业英语水平测试：大学英语六级，参考学分 5分/项，各学院（大学英语四级、艺术类专业英语三级、大学英语六级由各学院通过系统统一导入赋予学分）。
```

doc 12（无未命名列，p8）：

```
思想政治与道德修养 - 社会主义核心价值观教育：完成相应培训课时，参考学分 5分/学期，学工部（处）、各学院（每学期结束，由各学院团委通过系统管理端统一导入赋予学分）。
```

doc 11（通用模板）：

```
文章等级是SCI，第1 作者或通信作者是100，第2 作者是80，第3 作者是60。
```

> 方案 3 会在通用模板里把 `100` 补成 `100分`；方案 1 会把 doc 12 的多值学分格替换成“国家级一等奖18分/项；…”这样的自包含串。本方案先只做自然句骨架。
>
> ⚠️ p12 前 6 行的“类别/项目”是上一页合并单元格的延续；`forward_fill` 只在**同一张表内**传播，所以这些句子会从“院级：…”开始，而不是“技能特长 - 语言类证书 - …”。这不是错标；本方案不跨页搬行。后续如需完整前缀，单独让 `forward_fill` 在续页表继承上一张表的 `last`，不要在本方案里顺手改。

---

## 2. 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/ingestion/tables.py` | 表格提取、`col_spans`、表头继承、几何对齐、`serialize_groups` |
| `backend/app/ingestion/table_render.py` | **新建**：自然句渲染（doc 12 专用 + 通用） |
| `backend/app/ingestion/table_rules.py` | **新建（本方案只建空模块）**：方案 1/3 的纯单元格规则；本方案不实现 |
| `backend/app/ingestion/loaders/pdf.py` | 只改 `serialize_groups` 调用侧的返回/异常兼容；本方案不改逻辑 |
| `backend/tests/unit/test_tables.py` | 更新旧的键值断言，新增几何/自然句断言 |
| `backend/tests/unit/test_table_render.py` | **新建**：自然句单元测试 |
| `backend/tests/integration/test_loaders.py` | 更新 `GROUP_MARK`/键值断言 |

---

## 3. 任务

### Task 1: `col_spans` + `forward_fill` 掩码

**Files:**
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`

**Interfaces:**
- `RawTable` 新增：
  - `col_spans: list[tuple[float, float]] = field(default_factory=list)`
  - `col_headers: list[str | None] = field(default_factory=list)`
  - `dropped: bool = False`
- 新增 `_col_spans_from_cells(cells, col_count) -> list[tuple[float, float]]`
- `forward_fill(..., return_mask=False)`：`return_mask=True` 时返回 `(filled, mask)`，`mask[i][j]` 表示该格是填充出来的。

- [ ] **Step 1: 写失败测试**

```python
from app.ingestion.tables import RawTable, _col_spans_from_cells, forward_fill


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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/unit/test_tables.py::test_col_spans_from_real_p9_cells -v
```

Expected: FAIL（`_col_spans_from_cells` 不存在 / `return_mask` 不接受）。

- [ ] **Step 3: 实现**

```python
@dataclass
class RawTable:
    page: int
    bbox: tuple[float, float, float, float]
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    col_spans: list[tuple[float, float]] = field(default_factory=list)
    col_headers: list[str | None] = field(default_factory=list)
    dropped: bool = False


def _col_spans_from_cells(cells, col_count: int) -> list[tuple[float, float]]:
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
```

`extract_page_tables` 里给 `RawTable` 加：

```python
col_spans=_col_spans_from_cells(
    getattr(tab, "cells", None), getattr(tab, "col_count", 0)
),
```

`forward_fill` 增加掩码：

```python
def forward_fill(rows, *, skip_cols=frozenset(), return_mask=False):
    out: list[list[str]] = []
    mask: list[list[bool]] = []
    last: list[str] = []
    for row in rows:
        new = list(row)
        new_mask = [False] * len(new)
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
uv run pytest tests/unit/test_tables.py -k "col_spans or forward_fill" -v
```

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/tables.py backend/tests/unit/test_tables.py
git commit -m "[feat] 表格增加列坐标与 forward_fill 掩码"
```

---

### Task 2: 几何表头对齐

**Files:**
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`, `backend/tests/integration/test_loaders.py`

**Interfaces:**
- `X_TOL = 15.0`，`UNMATCHED_PENALTY = 1000.0`
- `align_headers_by_geometry(prev: RawTable, cur: RawTable) -> list[str | None]`
- `_validate_alignment(mapping: list[str | None], rows: list[list[str]]) -> bool`
- `inherit_headers` 现在会填 `t.col_headers`。

- [ ] **Step 1: 写失败测试**

用真实 spans 构造 p8 / p9 / p10 / p11：

```python
from app.ingestion.tables import RawTable, align_headers_by_geometry

P8 = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
P8_SPANS = [(30.7, 66.4), (66.4, 158.5), (158.5, 312.0),
            (312.0, 365.1), (365.1, 439.5), (439.5, 564.5)]
P9_SPANS = [(30.7, 66.4), (66.4, 158.5), (158.5, 225.3),
            (225.3, 313.0), (313.0, 371.1), (371.1, 442.3), (442.3, 564.5)]
P10_SPANS = [(30.6, 66.4), (66.4, 158.5), (158.5, 227.0),
             (227.0, 307.3), (307.3, 371.1), (371.1, 442.3), (442.3, 564.5)]


def _t(headers, spans):
    return RawTable(page=1, bbox=(0, 0, 0, 0), headers=list(headers),
                    col_headers=list(headers),
                    rows=[[""] * len(spans)], col_spans=list(spans))


def test_align_doc12_p9():
    prev = _t(P8, P8_SPANS)
    cur = _t([], P9_SPANS)
    assert align_headers_by_geometry(prev, cur) == [
        "课程类别", "课程项目", "课程内容", None,
        "参考学分", "开课单位", "获得学分方式",
    ]


def test_align_doc12_p10():
    prev = _t(P8, P8_SPANS)
    cur = _t([], P10_SPANS)
    assert align_headers_by_geometry(prev, cur)[3] is None
    assert align_headers_by_geometry(prev, cur)[4] == "参考学分"


def test_align_rejects_out_of_tolerance():
    prev = _t(P8, P8_SPANS)
    cur = _t([], [(x + 50, x2 + 50) for x, x2 in P9_SPANS])
    assert align_headers_by_geometry(prev, cur) == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/unit/test_tables.py -k align -v
```

Expected: FAIL（`align_headers_by_geometry` 不存在）。

- [ ] **Step 3: 实现**

```python
X_TOL = 15.0
UNMATCHED_PENALTY = 1000.0

_SCORE_RE = re.compile(r"\d+(?:\.\d+)?\s*分")
_DEPT_RE = re.compile(r"学院|团委|中心|党委|部（处）|教务处|处")


def _validate_alignment(mapping: list[str | None], rows: list[list[str]]) -> bool:
    checks = (("参考学分", _SCORE_RE, 0.9), ("开课单位", _DEPT_RE, 0.8))
    for header, pattern, threshold in checks:
        if header not in mapping:
            continue
        idx = mapping.index(header)
        vals = [r[idx].strip() for r in rows if idx < len(r) and r[idx].strip()]
        if vals and sum(bool(pattern.search(v)) for v in vals) / len(vals) < threshold:
            return False
    return True


def align_headers_by_geometry(prev: RawTable, cur: RawTable) -> list[str | None]:
    # ⚠️ 不能用 prev.headers：p9 继承后 headers 还是 6 个，但 col_headers 已经是 7 个
    #    （带一个 None）。p10 对齐必须以 p9 的 7 列映射为基准，否则 6 != 7 直接失败。
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
```

`inherit_headers` 改为填 `col_headers`：

```python
for t in tables:
    t.rows = normalize_grid(t.rows)
    if not t.rows:
        out.append(t)
        continue
    ncols = len(t.rows[0])
    if len(t.col_spans) != ncols:
        t.col_spans = []

    if t.headers:
        t.col_headers = list(t.headers) if len(t.headers) == ncols else []
        out.append(t)
        continue

    if is_continuation(t, prev, page_height=page_height):
        t.headers = list(prev.headers)
        mapping = align_headers_by_geometry(prev, t)
        t.col_headers = mapping
        out.append(t)
        continue

    if len(t.rows) > 1:
        t.headers = list(t.rows[0])
        t.rows = t.rows[1:]
        t.col_headers = list(t.headers) if len(t.headers) == ncols else []
        out.append(t)
        continue

    t.headers = []
    t.col_headers = []
    out.append(t)
```

- [ ] **Step 4: 加真实文档回归测试**

```python
import fitz
from app.ingestion.tables import extract_page_tables, inherit_headers

DOC12 = "../corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf"


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
```

- [ ] **Step 5: 跑测试并提交**

```bash
cd backend
uv run pytest tests/unit/test_tables.py -k "align or real_doc12" -v
git add backend/app/ingestion/tables.py backend/tests/unit/test_tables.py
git commit -m "[feat] 表格续页按列坐标对齐表头"
```

---

### Task 3: 自然句渲染器

**Files:**
- Create: `backend/app/ingestion/table_render.py`
- Create: `backend/tests/unit/test_table_render.py`

**Interfaces:**
- `render_sentence(raw_row: list[str], filled_row: list[str], col_headers: list[str | None]) -> str`
- `DOC12_HEADERS: set[str]`，`PLACEHOLDERS: set[str]`
- 内部：`_render_doc12`、`_render_generic`、`_escape`

- [ ] **Step 1: 写失败测试**

```python
from app.ingestion.table_render import render_sentence

DOC12_H = ["课程类别", "课程项目", "课程内容", None, "参考学分", "开课单位", "获得学分方式"]


def test_render_cet6_sentence():
    raw = [
        "技能特长", "语言类证书", "大学英语及专业英语水平测试",
        "大学英语六级", "5分/项", "各学院",
        "大学英语四级、艺术类专业英语三级、大学英语六级由各学院通过系统统一导入赋予学分",
    ]
    assert render_sentence(raw, raw, DOC12_H) == (
        "技能特长 - 语言类证书 - 大学英语及专业英语水平测试：大学英语六级，"
        "参考学分 5分/项，各学院（大学英语四级、艺术类专业英语三级、"
        "大学英语六级由各学院通过系统统一导入赋予学分）。"
    )


def test_render_no_leading_dash_when_category_and_project_empty():
    # p12 前几行是跨页合并单元格：类别/项目为空，内容从第 3 列开始。
    raw = ["", "", "院级", "学生会执行主席、团委兼职副书记",
           "8分/学年", "各学院团委", "由各学院团委通过系统统一导入赋予学分"]
    sentence = render_sentence(raw, raw, DOC12_H)
    assert not sentence.startswith(" - ")
    assert sentence.startswith("院级：学生会执行主席")


def test_render_p8_without_unnamed_column():
    raw = [
        "思想政治与道德修养", "社会主义核心价值观教育", "完成相应培训课时",
        "5分/学期", "学工部（处）、各学院",
        "每学期结束，由各学院团委通过系统管理端统一导入赋予学分",
    ]
    h = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
    assert render_sentence(raw, raw, h) == (
        "思想政治与道德修养 - 社会主义核心价值观教育：完成相应培训课时，"
        "参考学分 5分/学期，学工部（处）、各学院"
        "（每学期结束，由各学院团委通过系统管理端统一导入赋予学分）。"
    )


def test_render_uses_raw_value_for_unnamed_column():
    raw = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "",
           "18、15、12分/项", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
    filled = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
              "18、15、12分/项", "团委、体育部、各学院", "由活动主办单位通过系统统一导入赋予学分"]
    sentence = render_sentence(raw, filled, DOC12_H)
    assert "其他" not in sentence
    assert "获国家级奖项一、二、三等奖" in sentence


def test_render_generic_doc11():
    h = ["文章等级", "第1 作者或通信作者", "第2 作者", "第3 作者"]
    row = ["SCI", "100", "80", "60"]
    assert render_sentence(row, row, h) == (
        "文章等级是SCI，第1 作者或通信作者是100，第2 作者是80，第3 作者是60。"
    )


def test_render_empty_row():
    assert render_sentence([], [], []) == ""
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/unit/test_table_render.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 `table_render.py`**

```python
"""表格行的自然句渲染。

doc 12 的 6 列评分表用专用模板；其他表格用通用“列名是值”的句子。
⚠️ 本模块不导入 tables.py，避免循环依赖；只接收 list 行和表头。
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


def _render_doc12(raw_row, filled_row, col_headers) -> str:
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


def _render_generic(filled_row, col_headers) -> str:
    parts: list[str] = []
    for i, h in enumerate(col_headers):
        value = filled_row[i].strip() if i < len(filled_row) else ""
        if not value:
            continue
        parts.append(f"{h or f'列{i + 1}'}是{_escape(value)}")
    return "，".join(parts) + "。" if parts else ""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
uv run pytest tests/unit/test_table_render.py -v
```

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/table_render.py backend/tests/unit/test_table_render.py
git commit -m "[feat] 表格行改为自然句渲染"
```

---

### Task 4: 重写 `serialize_groups`

**Files:**
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_tables.py`, `backend/tests/integration/test_loaders.py`

**Interfaces:**
- `serialize_groups(table, *, budget) -> list[str]` 返回的每组以裸 `GROUP_MARK` 开头，正文是自然句。
- 删除 `SEP` / `KV` 常量及对应注释（如果测试仍引用，先更新测试）。

- [ ] **Step 1: 写失败测试**

```python
from app.ingestion.tables import GROUP_MARK, RawTable, serialize_groups


def _tbl(headers, rows, col_headers=None):
    return RawTable(page=1, bbox=(0, 0, 100, 100),
                    headers=list(headers), rows=rows,
                    col_headers=list(col_headers or []))


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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/unit/test_tables.py -k "bare_marker or natural_sentences or position_labels" -v
```

- [ ] **Step 3: 实现**

```python
from app.ingestion.table_render import render_sentence


def serialize_groups(table: RawTable, *, budget: int) -> list[str]:
    if not table.rows or table.dropped:
        return []
    ncols = len(table.rows[0])
    prh = [is_projected_row_header(r, ncols) for r in table.rows]
    filled, _mask = forward_fill(table.rows, return_mask=True)

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
        cost = len(line) + 1
        if cur and used + cost > body_budget:
            groups.append(GROUP_MARK + "\n" + "\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += cost
    if cur:
        groups.append(GROUP_MARK + "\n" + "\n".join(cur))
    return groups
```

- [ ] **Step 4: 更新旧测试**

- `test_every_row_carries_its_column_names`：改成自然句断言。
- `test_matching_header_count_still_pairs_by_name`：改成自然句。
- `test_mismatched_header_count_never_mislabels_columns`：断言 `列1是...`，并保留“不得出现错误列名”的断言。
- `test_projected_row_header_is_skipped_but_its_value_propagates`：改成自然句。
- `test_forward_fill_does_not_swallow_first_data_row`：改成自然句。
- `test_serialization_separators_never_break_a_chunk`：删除（`SEP` 不再存在），保留 `test_atomic_ranges_*`。
- `test_escape_cell_separators`：改成断言 `｜【】` 在自然句里被转义，正文没有裸 `GROUP_MARK`。
- `tables.py` 里的 `_ESCAPE` / `_escape` 已经移到 `table_render.py`；如果 `tables.py` 不再使用它们，删掉，避免两套转义逻辑。
- `tests/integration/test_loaders.py` 里 `hdr = GROUP_MARK + "课程类别｜..."` 改成裸 `GROUP_MARK`；涉及键值行的断言改成自然句。

- [ ] **Step 5: 跑全套表格测试**

```bash
cd backend
uv run pytest tests/unit/test_tables.py tests/unit/test_table_render.py tests/integration/test_loaders.py -v
```

Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add backend/app/ingestion/tables.py backend/app/ingestion/table_render.py backend/tests/unit/test_tables.py backend/tests/integration/test_loaders.py
git commit -m "[feat] 表格序列化统一为自然句并保留裸哨兵"
```

---

### Task 5: 真实文档回归 + 离线 A/B

**Files:**
- Modify: `docs/superpowers/plans/2026-10-16-table-column-recovery.md`（记录结果）
- Optional new: `backend/tools/table_render_ab.py`（一次性对比脚本，跑完可删）

- [ ] **Step 1: 真实文档回归**

```python
import fitz
from app.ingestion.tables import extract_page_tables, inherit_headers, serialize_groups


def test_doc12_cet6_sentence():
    doc = fitz.open("../corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf")
    prev = None
    try:
        for page_no in range(8, 13):
            page = doc.load_page(page_no - 1)
            tables = inherit_headers(
                extract_page_tables(page), prev, page_height=page.rect.height)
            prev = tables[0]
        text = "\n".join(serialize_groups(prev, budget=500))
    finally:
        doc.close()
    assert "大学英语及专业英语水平测试：大学英语六级，参考学分 5分/项" in text


def test_doc11_generic_sentence():
    doc = fitz.open("../corpus/guet/11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf")
    page = doc.load_page(2)           # p3
    tables = inherit_headers(extract_page_tables(page), None, page_height=page.rect.height)
    text = "\n".join(serialize_groups(tables[0], budget=500))
    assert "文章等级是SCI，第1 作者或通信作者是100" in text
    doc.close()
```

> 注意：p12 的 `inherit_headers` 传 `prev=None` 时拿不到上一页表头，`col_headers` 会是空；上面这条测试只验证“自然句能跑通”，完整的跨页对齐用 `test_real_doc12_col_headers_align`。如果需要直接测 p12 的模板，构造 `RawTable` 并设置 `col_headers`。

- [ ] **Step 2: 离线 A/B**

用**同一批候选**分别渲染两种形态，避免重建索引：

1. 用当前索引取出 17 题（`backend/eval_sets/二课表格口语题.json`）和 10 题（`backend/eval_sets/第二课堂评分表专项.json`）的候选块。
2. 对每个候选块，找到它在原始表格中的行；分别生成：
   - **形态 A**：旧键值行 + 完整 `【表头】列名`；
   - **形态 B**：自然句 + 裸 `【表头】`。
3. 用当前 reranker（`rerank_sync`）对同一 query 的 A/B 文本打分，记录目标块排名和分数。
4. 同时跑 Jina（`min_score=0.08`）和 bge（`0.3`）两套，结果分开记录。

**通过线：**

| 指标 | 通过线 |
|---|---|
| 表格题目标块 rerank 排名 | B 不差于 A 的题数 ≥ 80%；没有任何一题比 A 差 2 名以上 |
| 目标块是否过当前后端阈值 | 不少于 A |
| doc 11 非表格问题 | 端到端答对/拒答不变差 |
| chunk 数 / 组长度 | 总字数增幅 ≤15%；单组不超过 2×budget |

不达标 → 回到设计，不重建索引。

- [ ] **Step 3: 记录结果并提交**

把 A/B 表格（后端、题目、A 排名/分、B 排名/分）写进本文档 §4；提交：

```bash
git add docs/superpowers/plans/2026-10-16-table-column-recovery.md
git commit -m "[docs] 记录自然句渲染的离线 A/B 结果"
```

---

## 4. 验证门禁（不通过不上线）

**2026-10-18 实施记录（自然句落地后，真实管线口径）：**

| 文档 | chunk 数 | 总字数 | 表格组数 |
|---|---|---|---|
| doc 11 | 29 | 5810 | 12 |
| doc 12 | 34 | 12055 | 20 |

> 对照旧键值格式：doc 11 29/5905，doc 12 42/15092。自然句让 doc 12 的 chunk 数和字数都明显下降；doc 11 基本不变。
> 用户要求施工完成后直接做两轮 bge/jina 对比评测；原先的“旧渲染 vs 新渲染”离线 A/B 合并进这两轮评测，不再单独跑。

| # | 检查 | 通过线 |
|---|---|---|
| 1 | 几何对齐单测（p8→p9/p10/p11） | 6 个表头全部对上，第 4 列为 `None` |
| 2 | 真实 doc 12 p8–p12 的 `col_headers` | 与 §1.3 逐字一致 |
| 3 | 自然句单测 | CET6、p8、p10、doc 11 四条逐字通过 |
| 4 | 旧键值断言 | 全部改成自然句后全绿；正文不再出现 `｜` |
| 5 | 原子区间 | `table_atomic_ranges` 仍能覆盖每个组；组不被切碎 |
| 6 | 离线 A/B | 满足 §3 Task 5 的四条通过线 |
| 7 | 真实管线 chunk | 用带页边界 + atomic 的 `chunk_text` 记录改动前后；不出现单组异常膨胀 |

---

## 5. 风险与回退

| 风险 | 影响 | 缓解 |
|---|---|---|
| 列坐标不可用（PyMuPDF 版本/表格识别失败） | 新表拿不到 `col_spans` | 自动退回 `列N` 位置标签；不猜 |
| 几何对齐把中间新增列对错 | 错标列义 | 15pt 容差 + 内容校验（分值/部门列）；失败即回退 |
| 自然句让 chunk 变长 | 组数/字数变化 | 保留单行超预算自成一组；A/B 门禁限制总字数增幅 |
| doc 11 通用自然句影响原有分数 | 非表格题变差 | 离线 A/B 加 doc 11 对照组；不达标不上线 |
| 裸哨兵仍被清洗规则误删 | 表格组失去原子保护 | `clean.py` 的 `startswith(GROUP_MARK)` 豁免保留；测试钉住 |
| 合并单元格填充污染语义 | 无名列写出假值 | 无名列只用 `raw_row`；`forward_fill` 掩码留给方案 1/3 |
