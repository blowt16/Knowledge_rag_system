# 空白表单排除 + 裸数字补单位 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 doc 11 第 8 页的空白申请表整表从索引里剔除；给非表单表格里真正的分值列补上“分”，不让 `90` 这种裸数字进入检索。

**Architecture:** `table_rules.py` 增加两个纯函数：`is_application_form(rows)` 用“表头词 + 结构信号”判定表单；`infer_score_columns(rows, headers)` + `apply_score_units(...)` 按列推断分值列并补单位。`RawTable.dropped` 标记表单表；`_text_with_tables` 用**所有**表格的 bbox 剔除拍平正文，但只序列化非 dropped 的表。自然句渲染接在方案乙的 `table_render.py` 上，不引入配置开关。

**Tech Stack:** Python 3.13、PyMuPDF、pytest。

**Spec:** 本文档 §1–§4。依赖 `2026-10-16-table-column-recovery.md`（自然句 + `RawTable.dropped/col_headers`）。

---

## Global Constraints

- 分支：`refactor/campus-rag`；不直接提交 `main`。
- doc 11 语料基线（已确认）：当前工作区 PDF，409721 字节，md5 `d4382dff21353cac39112285452bdef8`。
- **表单残留已确认：保留。** 只剔除表格主体；表格 bbox 外的“合计 77”“总审核人”等正文不动。
- 渲染统一自然句；本方案不加配置开关。
- 只改 PDF 本地文字层路径；OCR 路径不动。
- 真实管线 `chunk_text` 必须带页边界 + atomic ranges。**不要沿用旧键值格式下的 29→21/27 数字**；方案乙落地后重新记录基线，再跑门禁。
- 当前默认后端 Jina `min_score=0.08`；bge 为 `0.3`；A/B 两套都记录。
- 提交信息：`[类型] 简述`。

---

## Review Focus

- **表单误伤：** 只看“6 个词命中 ≥2”会误删普通表格；必须加结构信号。
- **bbox 剔除：** 把表单表从 `tables` 列表删掉会导致它的拍平文字仍进正文；要“保留 bbox、只跳过序列化”。
- **残留：** 表格外框的“合计 77”按决定保留；测试只断言表单主体不在，不断言“一个词都没有”。
- **补单位误伤：** 序号/编号/人数/年份/数量列不能补“分”；`100（团队其他成员50）`、`5 分`、`2 分/次` 不能动。
- **填充值：** `forward_fill` 填出来的空格不加“分”；p3 表1 的 30/20/20、p4 表1 的 10 是填充值。
- **doc 12：** 表单 0 张、裸整数 0 个；本方案不能改变 doc 12 的自然句输出。

---

## 1. A：空白申请表排除

### 1.1 它是什么

doc 11 第 8 页是一张**空白申请表模板**：

```
学号 |  |  |  | 专业 |  |  |
姓名 |  |  |  | 课号 |  |  |
序号 | 类别 | 认定内容 | 成绩 | 审核人签字
例1 … 例6
合计 | 77
```

它不是政策条款，是让申请人填的模板；对问答没有价值。

### 1.2 现状问题

- 表头名是表单字段（学号/姓名/专业/课号），不是列义；
- 空列名产生 `：值`；
- `例6` 被 `forward_fill` 重复多次；
- `合计` 被当数据。

### 1.3 多信号判据

```python
FORM_WORDS = ("审核人签字", "学号", "姓名", "合计", "专业", "课号")
EXAMPLE_RE = re.compile(r"^例\d+$")

def is_application_form(rows: list[list[str]]) -> bool:
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
```

实测（当前语料）：

| 文档 | 表 | 判定 | 依据 |
|---|---|---|---|
| doc 11 | p3/p4/p5/p6 共 7 张 | 正常 | 词命中 0–1；结构信号 0 |
| doc 11 | **p8 表1** | **表单** | 词命中 6；空表头 6；例N 行 6；有合计行 |
| doc 12 | 全部 5 张 | 正常 | 词命中 0–1；结构信号 0 |

注意：

- p6 表0 会命中 1 个“专业”，但结构信号 0，**不会**被删。
- 判据在 `extract_page_tables()` 之后、`inherit_headers()` 之前执行，此时表头行还在 rows 里，能数到 6 个空表头。
- 代码里没有现成的 `normalize(cell)`；本方案用 `"".join(c.split())` 去空白，不引入新函数。

### 1.4 剔除模型（关键）

**不要**把表单表从 `tables` 列表里删掉。`_text_with_tables` 的 `table_boxes` 是从 `tables` 算的；删掉表，bbox 就不在排除列表里，拍平文字仍然会进正文。

正确做法：

1. 所有检测到的表都保留在 `tables` 里；
2. 给表单表设 `t.dropped = True`；
3. `table_boxes = [t.bbox for t in tables]`——**包括 dropped**，用所有 bbox 剔除拍平块；
4. 只对 `not t.dropped` 的表调用 `serialize_groups`；
5. `prev_table` 只更新为最后一个**非 dropped** 的表。

实测（旧键值格式，仅作方向参考）：

| 做法 | chunk 数 | 总字数 | 表单主体 |
|---|---|---|---|
| 现状 | 29 | 5905 | 在 |
| 从列表删除（错误） | 27 | 5531 | **仍在**（一个 chunk 含“审核人签字/例1/合计”） |
| 保留 bbox、只跳过序列化（正确） | 26 | 5272 | 不在；只剩表外“合计 77”等 138 字 |

> 方案乙换成自然句后 chunk 数会变；门禁必须重新记录基线，不要写死 29/26。

### 1.5 残留处理

按已确认决定：**保留**表格 bbox 外的“合计 77”“总审核人”“支撑材料”等正文。测试只断言：

- `学号`、`姓名`、`例1`…`例6`、`审核人签字` 不在正文；
- 页面标题和“合计 77”可以保留；
- 不因为残留再写一套页面级删除规则。

---

## 2. B：裸数字补单位

### 2.1 现状

doc 11 非表单表的纯整数格 163 个（原始行）。例如：

```
文章等级：SCI｜第1 作者或通信作者：100｜第2 作者：80｜第3 作者：60
```

`100` 是 100 分，但字面上没有“分”。

### 2.2 不按“所有纯整数”补，按列推断

```python
PURE_INT = re.compile(r"^\d+$")
NON_SCORE_HEADERS = ("序号", "编号", "人数", "年份", "数量", "页码")

def infer_score_columns(rows: list[list[str]], headers: list[str | None]) -> set[int]:
    if not rows:
        return set()
    ncols = len(rows[0])
    out: set[int] = set()
    for i in range(ncols):
        header = (headers[i] if i < len(headers) and headers[i] else "")
        if any(w in header for w in NON_SCORE_HEADERS):
            continue
        vals = [r[i].strip() for r in rows if i < len(r) and r[i].strip()]
        if not vals:
            continue
        ints = [v for v in vals if PURE_INT.match(v)]
        if len(ints) / len(vals) < 0.8:
            continue
        nums = [int(v) for v in ints]
        if all(0 <= n <= 1000 for n in nums):
            out.add(i)
    return out


def apply_score_units(filled_row, mask_row, score_cols) -> list[str]:
    out = list(filled_row)
    for i in score_cols:
        if i >= len(out) or i >= len(mask_row) or mask_row[i]:
            continue
        value = out[i].strip()
        if PURE_INT.match(value):
            out[i] = value + "分"
    return out
```

规则：

- 列内 ≥80% 的非空值是纯整数，且都在 0–1000 → 视为分值列；
- 表头含序号/编号/人数/年份/数量/页码 → 整列不补；
- `forward_fill` 填出来的格子（`mask_row[i] == True`）不补；
- `100（团队其他成员50…）`、`5 分`、`2 分/次` 不是纯整数，不动；
- 只补“分”，不推断 `学时`/`次`；本语料没有需要推断其他单位的格子。

### 2.3 数量口径

| 指标 | 数值 |
|---|---|
| 原始纯整数格 | **163** |
| `forward_fill` 后纯整数格 | **167** |
| 实际补“分”的格 | **163**（4 个填充格跳过） |

4 个填充格：

- p3 表1：`发明专利申请受理并公开` 第4及以后 30（从上一行填）、`实用新型…申请并受理` 第4及以后 20、`软件著作权授权` 第4及以后 20；
- p4 表1：`（校级）…` 第3 名及之后 10。

这些是 `forward_fill` 的既有行为；本方案不修它，只是不给它们补“分”，避免把猜出来的值写得更像真的。

### 2.4 目标自然句

doc 11 通用模板：

```
文章等级是SCI，第1 作者或通信作者是100分，第2 作者是80分，第3 作者是60分。
```

```
竞赛类别是A类竞赛，获奖等级是一等奖，第1 名是100分，第2 名是90分，第3 名是75分，第4 名-第6 名是60分。
```

```
类别是全国大学英语六级考试（CET6）成绩≥425，分值是50分。
```

p6 表1（已带单位/括号）保持：

```
类别是创业实践，标准是创办企业并取得营业执照且通过学校审核入驻，提供不少于一年的流水，分值是100（团队其他成员50，入驻场地提供团队成员证明）。
```

---

## 3. 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/ingestion/table_rules.py` | 增加 `is_application_form`、`infer_score_columns`、`apply_score_units` |
| `backend/app/ingestion/tables.py` | `serialize_groups` 传入 `score_cols`/`mask_row`；`RawTable.dropped` 已在方案乙加好 |
| `backend/app/ingestion/table_render.py` | `render_sentence` 增加 keyword-only `score_cols`/`mask_row`；`_render_generic` 调用 `apply_score_units` |
| `backend/app/ingestion/loaders/pdf.py` | `_text_with_tables`：所有 bbox 剔除、只序列化非 dropped、prev 只取非 dropped |
| `backend/tests/unit/test_table_rules_form.py` | **新建**：表单判定 |
| `backend/tests/unit/test_table_rules_units.py` | **新建**：分值列推断/补单位 |
| `backend/tests/unit/test_tables.py` | 更新 `serialize_groups` 的单位断言 |
| `backend/tests/integration/test_loaders.py` | 真实 doc 11 p8 剔除、doc 12 不变 |

---

## 4. 任务

### Task 1: 多信号表单判定

**Files:**
- Modify: `backend/app/ingestion/table_rules.py`
- Create: `backend/tests/unit/test_table_rules_form.py`

**Interfaces:**
- `is_application_form(rows: list[list[str]]) -> bool`

- [ ] **Step 1: 写失败测试**

```python
from app.ingestion.table_rules import is_application_form


def test_form_table_detected():
    rows = [
        ["学号", "", "", "", "专业", "", "", ""],
        ["姓名", "", "", "", "课号", "", "", ""],
        ["序号", "类别", "", "认定内容", "", "成绩", "", "审核人签字"],
        ["例1", "科研成果", "", "论文", "", "25", "", ""],
        ["例2", "科研成果", "", "专利", "", "10", "", ""],
        ["例3", "科研训练", "", "大创", "", "20", "", ""],
        ["合计", "", "", "", "", "77", "", ""],
    ]
    assert is_application_form(rows) is True


def test_normal_table_with_name_and_major_not_dropped():
    rows = [
        ["姓名", "专业", "成绩"],
        ["张三", "计算机", "90"],
    ]
    assert is_application_form(rows) is False


def test_form_words_without_structure_not_dropped():
    rows = [
        ["学号", "姓名", "备注"],
        ["001", "张三", "正常表"],
    ]
    assert is_application_form(rows) is False
```

- [ ] **Step 2: 跑失败**

```bash
cd backend
uv run pytest tests/unit/test_table_rules_form.py -v
```

- [ ] **Step 3: 实现**

```python
FORM_WORDS = ("审核人签字", "学号", "姓名", "合计", "专业", "课号")
EXAMPLE_RE = re.compile(r"^例\d+$")


def is_application_form(rows: list[list[str]]) -> bool:
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
```

- [ ] **Step 4: 真实语料回归**

```python
import fitz
from app.ingestion.tables import extract_page_tables
from app.ingestion.table_rules import is_application_form

DOC11 = "../corpus/guet/11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf"
DOC12 = "../corpus/guet/12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf"


def test_only_doc11_p8_is_form():
    for path, expected_pages in ((DOC11, {8}), (DOC12, set())):
        doc = fitz.open(path)
        try:
            for page_no in range(1, doc.page_count + 1):
                page = doc.load_page(page_no - 1)
                for t in extract_page_tables(page):
                    assert is_application_form(t.rows) == (page_no in expected_pages)
        finally:
            doc.close()
```

- [ ] **Step 5: 跑测试并提交**

```bash
cd backend
uv run pytest tests/unit/test_table_rules_form.py -v
git add backend/app/ingestion/table_rules.py backend/tests/unit/test_table_rules_form.py
git commit -m "[feat] 多信号识别空白申请表"
```

---

### Task 2: 保留 bbox、只跳过序列化

**Files:**
- Modify: `backend/app/ingestion/loaders/pdf.py`
- Test: `backend/tests/integration/test_loaders.py`

**Interfaces:**
- `_text_with_tables` 使用 `t.dropped`；
- `prev_table` 只更新为最后一个非 dropped 表；
- `table_boxes` 包含所有表（含 dropped）。

- [ ] **Step 1: 写失败测试**

```python
from app.ingestion.loaders.pdf import load_pdf

DOC11 = "../corpus/guet/11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf"


def test_doc11_p8_form_body_removed_but_residual_kept():
    result = load_pdf(DOC11)
    text = next(p.text for p in result.pages if p.page == 8)
    for word in ("学号", "姓名", "例1", "例6", "审核人签字"):
        assert word not in text, f"表单主体 {word} 仍在正文"
    assert "合计" in text and "77" in text, "表外残留按决定保留"
```

- [ ] **Step 2: 跑失败**

```bash
cd backend
uv run pytest tests/integration/test_loaders.py::test_doc11_p8_form_body_removed_but_residual_kept -v
```

Expected: FAIL（表单主体仍在）。

- [ ] **Step 3: 修改 `_text_with_tables`**

```python
from app.ingestion.table_rules import is_application_form

tables = extract_page_tables(page)
for t in tables:
    t.dropped = is_application_form(t.rows)
tables = inherit_headers(tables, prev_table, page_height=page.rect.height)
if not tables:
    return fallback, boxes, prev_table

table_boxes = [t.bbox for t in tables]   # 所有表，含 dropped

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

items: list[tuple[float, int, str]] = []
for blk, box in zip(kept_blocks, kept_boxes):
    items.append((box["top"], 0, blk))
for t in tables:
    if t.dropped:
        continue
    for g in serialize_groups(t, budget=chunk_size_hint()):
        items.append((t.bbox[1], 1, g))
items.sort(key=lambda x: (x[0], x[1]))

out: list[str] = []
for _top, kind, payload in items:
    if kind == 1:
        if out:
            out.append("")
        out.append(payload)
        out.append("")
    else:
        out.append(payload)

last_kept = next((t for t in reversed(tables) if not t.dropped), prev_table)
return re.sub(r"\n{3,}", "\n\n", "\n".join(out)), kept_boxes, last_kept
```

- [ ] **Step 4: 补“全页表都被 dropped”测试**

doc 11 p8 只有这一张表，正好就是“全页表都被 dropped”的真实案例：

```python
def test_all_dropped_page_keeps_title_and_residual():
    result = load_pdf(DOC11)
    text = next(p.text for p in result.pages if p.page == 8)
    assert "桂林电子科技大学《创新创业拓展》课程" in text
    assert "学分认定申请表" in text
    assert "合计" in text and "77" in text
    for word in ("学号", "姓名", "例1", "审核人签字"):
        assert word not in text
```

- [ ] **Step 5: 跑测试并提交**

```bash
cd backend
uv run pytest tests/integration/test_loaders.py -v
git add backend/app/ingestion/loaders/pdf.py backend/tests/integration/test_loaders.py
git commit -m "[fix] 表单表保留 bbox 剔除、跳过序列化"
```

---

### Task 3: 分值列推断与补单位

**Files:**
- Modify: `backend/app/ingestion/table_rules.py`
- Create: `backend/tests/unit/test_table_rules_units.py`

**Interfaces:**
- `infer_score_columns(rows, headers) -> set[int]`
- `apply_score_units(filled_row, mask_row, score_cols) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑失败**

```bash
cd backend
uv run pytest tests/unit/test_table_rules_units.py -v
```

- [ ] **Step 3: 实现**

```python
PURE_INT = re.compile(r"^\d+$")
NON_SCORE_HEADERS = ("序号", "编号", "人数", "年份", "数量", "页码")


def infer_score_columns(rows, headers):
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


def apply_score_units(filled_row, mask_row, score_cols):
    out = list(filled_row)
    for i in score_cols:
        if i >= len(out) or i >= len(mask_row) or mask_row[i]:
            continue
        value = out[i].strip()
        if PURE_INT.match(value):
            out[i] = value + "分"
    return out
```

- [ ] **Step 4: 跑测试并提交**

```bash
cd backend
uv run pytest tests/unit/test_table_rules_units.py -v
git add backend/app/ingestion/table_rules.py backend/tests/unit/test_table_rules_units.py
git commit -m "[feat] 按列推断分值并补单位"
```

---

### Task 4: 接进自然句渲染

**Files:**
- Modify: `backend/app/ingestion/table_render.py`
- Modify: `backend/app/ingestion/tables.py`
- Test: `backend/tests/unit/test_table_render.py`, `backend/tests/unit/test_tables.py`

**Interfaces:**
- `render_sentence(raw_row, filled_row, col_headers, *, score_cols=frozenset(), mask_row=None)`
- `serialize_groups` 调用前算 `score_cols = infer_score_columns(table.rows, col_headers)`，并把 `mask` 传下去。

- [ ] **Step 1: 写失败测试**

```python
def test_render_doc11_adds_units():
    h = ["文章等级", "第1 作者或通信作者", "第2 作者", "第3 作者"]
    row = ["SCI", "100", "80", "60"]
    assert render_sentence(row, row, h, score_cols={1, 2, 3}) == (
        "文章等级是SCI，第1 作者或通信作者是100分，第2 作者是80分，第3 作者是60分。"
    )


def test_render_doc11_skips_filled_cell_unit():
    h = ["专利类型", "第1 作者", "第2 作者", "第3 作者", "第4 及以后"]
    raw = ["发明专利申请受理并公开", "50", "30", "10", ""]
    filled = ["发明专利申请受理并公开", "50", "30", "10", "30"]
    mask = [False, False, False, False, True]
    sentence = render_sentence(raw, filled, h, score_cols={1, 2, 3, 4}, mask_row=mask)
    assert "第4 及以后是30。" in sentence
    assert "第4 及以后是30分" not in sentence
```

- [ ] **Step 2: 修改 `render_sentence`/`_render_generic`**

```python
def render_sentence(raw_row, filled_row, col_headers, *,
                    score_cols=frozenset(), mask_row=None) -> str:
    if DOC12_HEADERS.issubset({h for h in col_headers if h}):
        return _render_doc12(raw_row, filled_row, col_headers)
    return _render_generic(filled_row, col_headers,
                           score_cols=score_cols, mask_row=mask_row)


def _render_generic(filled_row, col_headers, *,
                    score_cols=frozenset(), mask_row=None) -> str:
    values = apply_score_units(
        filled_row, mask_row or [False] * len(filled_row), score_cols)
    parts: list[str] = []
    for i, h in enumerate(col_headers):
        value = values[i].strip() if i < len(values) else ""
        if not value:
            continue
        parts.append(f"{h or f'列{i + 1}'}是{_escape(value)}")
    return "，".join(parts) + "。" if parts else ""
```

`serialize_groups`（把方案乙里的 `_mask` 改成 `mask`，并在循环里传下去）：

```python
from app.ingestion.table_rules import infer_score_columns

filled, mask = forward_fill(table.rows, return_mask=True)
score_cols = infer_score_columns(table.rows, col_headers)

lines: list[str] = []
for raw_row, filled_row, mask_row, is_prh in zip(
        table.rows, filled, mask, prh):
    if is_prh:
        continue
    sentence = render_sentence(
        raw_row, filled_row, col_headers,
        score_cols=score_cols, mask_row=mask_row,
    )
    if sentence:
        lines.append(sentence)
```

- [ ] **Step 3: 跑测试**

```bash
cd backend
uv run pytest tests/unit/test_table_render.py tests/unit/test_tables.py -v
```

⚠️ 方案乙 Task 5 里的 `test_doc11_generic_sentence` 期望 `文章等级是SCI，第1 作者或通信作者是100`；本任务落地后它应改成 `…是100分`。这是预期变化，不是回归。

- [ ] **Step 4: 提交**

```bash
git add backend/app/ingestion/table_render.py backend/app/ingestion/tables.py backend/tests/unit/test_table_render.py backend/tests/unit/test_tables.py
git commit -m "[feat] 自然句渲染接入分值单位"
```

---

### Task 5: 真实管线基线 + A/B

- [ ] **Step 1: 重新记录 doc 11/12 基线**

在方案乙/方案 1 都落地后，用真实管线记录：

```
load_pdf → build_normalized_text → chunk_text(boundaries=页边界, atomic=table_atomic_ranges)
```

记录：

- doc 11 chunk 数、总字数；
- doc 12 chunk 数、总字数；
- doc 11 第 8 页正文是否还含表单主体；
- doc 12 自然句是否逐字不变。

- [ ] **Step 2: A/B**

- A：不含表单剔除/补单位的自然句；
- B：含本方案。

通过线：

| 指标 | 通过线 |
|---|---|
| doc 11 表单主体 | B 中不存在学号/姓名/例1/审核人签字 |
| doc 11 表格题目标块排名 | B 不差于 A |
| 非分值整数列 | B 中序号/人数/年份列不出现“分” |
| doc 12 | chunk 数、自然句与 A 一致 |
| chunk 数 | B 比 A 少（少掉的正是表单组）；不出现异常碎片 |

- [ ] **Step 3: 记录并提交**

把 A/B 和基线写进本文档 §5；提交：

```bash
git add docs/superpowers/plans/2026-10-16-table-form-exclusion-and-units.md
git commit -m "[docs] 记录表单剔除与补单位的基线/A-B"
```

---

## 5. 验证门禁

| # | 检查 | 通过线 |
|---|---|---|
| 1 | 表单判据 | doc11 p8 剔除；其余 12 张保留 |
| 2 | 表单主体 | 学号/姓名/例1–例6/审核人签字不在正文 |
| 3 | 残留 | “合计 77”按决定保留；不写页面级删除 |
| 4 | bbox | dropped 表的拍平块也被剔除；全 dropped 页不重复正文 |
| 5 | 补单位 | 163 个原始纯整数格补“分”；4 个填充格跳过 |
| 6 | 非分值列 | 序号/编号/人数/年份/数量零误补 |
| 7 | 已带单位/括号 | `5 分`、`2 分/次`、`100（…）` 不变 |
| 8 | doc 12 | 表单 0、裸整数 0；自然句输出不变 |
| 9 | 真实管线 chunk | 按重算基线比较；表单组消失，无异常碎片 |
| 10 | 离线 A/B | doc11 表格题不差；不出现在 doc12 上的回归 |

---

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 表单判据误伤 | 删掉正常表 | 多信号 + 结构门槛；13 张表回归 |
| bbox 处理错 | 表单文字仍入库/正文被多删 | 保留 bbox、只跳过序列化；真实 p8 测试 |
| 填充值补单位 | 把猜的值写得更真 | 用 `forward_fill` 掩码跳过；4 个格子钉住 |
| 非分值整数列 | 序号/人数被补“分” | 表头黑名单 + 0–1000 范围 + 列级 80% 门槛 |
| 残留“合计 77” | 轻微噪声 | 已确认保留；不影响表单主体剔除 |
| doc 12 被波及 | 自然句/单位变化 | doc12 走专用模板；测试断言逐字不变 |
