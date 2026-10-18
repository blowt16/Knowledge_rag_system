# 多值学分展开 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 doc 12 里“一格多个等级、另一格多个分值”的 16 行展开成自包含的“等级-分值”对，让检索和生成都能直接读到“国家级二等奖15分/项”，不再靠模型跨格数位置。

**Architecture:** 新增 `backend/app/ingestion/table_rules.py` 存放纯单元格规则；`expand_multi_value_row(raw_row, col_headers) -> dict[int, str]` 只返回“学分列下标 → 展开文本”。自然句渲染器 `table_render._render_doc12` 检测到这个映射后，用展开文本替换原来的“未命名等级 + 参考学分”两个字段。没有配置开关；doc 12 的多值行永远走展开，展开失败就保持原句。

**Tech Stack:** Python 3.13、pytest；不引入新依赖。

**Spec:** 本文档 §1–§4。依赖 `2026-10-16-table-column-recovery.md` 的 `table_render.py` 和 `col_headers`。

---

## Global Constraints

- 分支：`refactor/campus-rag`；不直接提交 `main`。
- 语料基线：当前工作区 doc 12，md5 `5dc147e1d772b32bdae534ec3dc98279`；doc 11 不受本方案影响。
- 渲染已经是自然句（方案乙）；本方案不再引入键值格式，也不加配置开关。
- **限定词规则（已确认）：每项都带。**
  - p9/p10/p11 的“特等奖/一等奖/…”前面加“国家级/区级/校级/院级”；
  - p8 的等级项本身就是“区级/校级/院级”，不再重复加前缀。
- 只处理 doc 12 的 6 列表头 schema（含 `参考学分`、`开课单位`、`获得学分方式`）；其他表格不展开。
- 展开失败一律原样返回，不猜。
- 长度上限：`MAX_EXPANSION_RATIO = 5.0`。实测带限定词后最大 4.3 倍（`8、5、3、2分/项` → 4 个校级奖项）；不要沿用原来的 3 倍，会误杀。
- 真实管线/离线 A/B 的口径同方案乙：带页边界 + atomic ranges，按当前 Jina（0.08）/ bge（0.3）分别记录。
- 提交信息：`[类型] 简述`。

---

## Review Focus

- **p8 的等级项是“区级/校级/院级”本身**：不能再加“区级区级…”。
- **p10 的等级在左邻 2 格**：左邻 1 是 `其他`；必须能找到“获国家级奖项一、二、三等奖”。
- **p9/p11 的范围词在内容格**：等级格只有“特等奖/一等奖/金奖”，范围词在“获国家级奖项”里，不能只从等级格找。
- **`其他` / 空等级格**：不能把 `forward_fill` 填出来的 `其他` 当成真实等级。
- **单项/非等级多值**：`5分/项`、岗位名多值、项数不等都必须原样不动。
- **输出重复**：展开后自然句里不能再出现“参考学分 18、15、12分/项”或重复的等级格。
- **文本长度**：4 个值 + 每项限定词可能到 4.3 倍；不能再用 3 倍阀。

---

## 1. 适用范围与边界

### 1.1 会处理的

同时满足：

1. 学分格全串匹配 `^[\d、.\s]+分[/／]\S*$`；
2. 按 `、` / `，` 切分后 ≥2 个值；
3. 左邻 1 格、再左邻 2 格里存在等级格（p10 的左邻 1 是 `其他`，靠左邻 2）；
4. 每个等级项要么是奖项（`特等奖|金奖|银奖|铜奖|优胜奖|[一二三四五](?:等奖)?`），要么是范围项（`国家级|区级|校级|院级`）；
5. 等级项数 == 学分值数；
6. 各值的单位后缀一致（如都是 `分/项` 或都是 `分/次`）；
7. 展开后长度 ≤ 原格 5 倍。

### 1.2 不处理的

| 情况 | 行为 |
|---|---|
| 单值学分 `5分/项` | 不展开（doc 12 有 82 行属于这类） |
| 岗位名多值（`各校级学生组织副职、各学生社团副职`） | 不展开 |
| 等级 3 项、学分 1 项 | 不展开 |
| 单位后缀混用（`15分/项、10分/次`） | 不展开 |
| 等级项里混入非等级词 | 不展开 |
| doc 11 的全部表 | 实测 0 行，不受影响 |

### 1.3 覆盖

| 文档 | 数据行 | 多值行 |
|---|---|---|
| doc 12 | 98 | **16** |
| doc 11 | 56 | 0 |

---

## 2. 解析与展开规则

### 2.1 等级词

```python
GRADE_TOKEN_RE = re.compile(
    r"(?:特等奖|金奖|银奖|铜奖|优胜奖|[一二三四五](?:等奖)?)$"
)
SCOPE_RE = re.compile(r"(国家级|区级|校级|院级)")
SCOPE_ITEM_RE = re.compile(r"^(?:国家级|区级|校级|院级)$")
CREDIT_RE = re.compile(r"^[\d、.\s]+分[/／]\S*$")
```

注意：

- `(?:...)` 必须把整组锚在 `$` 上。原方案写成 `(特等奖|...|[一二三四五](等奖?)?)$`，`$` 只对最后一个分支生效，`国家级奖项` 会被误认成等级。
- `[一二三四五](?:等奖)?` 要允许裸的 `一`、`二`、`三`；`一、二、三等奖` 是省略写法。

### 2.2 限定词

- 如果所有等级项都是**范围项**（p8：`区级、校级、院级` / `国家级、区级、校级、院级`）：
  - 不额外加前缀；每项本身就是范围。
- 否则（奖项项）：
  - 先从等级格里找范围（p10：`获国家级奖项一、二、三等奖`）；
  - 找不到，再从等级格左边一格找（p9/p11：内容格 `获国家级奖项`）；
  - 找到后，给每个清洗后的奖项项加前缀。
- 清洗：
  - 裸 `一/二/三` → `一等奖/二等奖/三等奖`；
  - `获国家级奖项一` → `一等奖`；
  - `特等奖（金奖）` 保留括号；
  - 已带前缀的项不重复加。

### 2.3 展开

```
学分值：["15", "10", "5分/次"]
等级：  ["区级", "校级", "院级"]
单位：  "/次"
输出：  区级15分/次；校级10分/次；院级5分/次
```

```
学分值：["30", "20", "15", "10分/项"]
等级：  ["特等奖（金奖）", "一", "二", "三等奖"]
范围：  国家级（来自内容格“获国家级奖项”）
输出：  国家级特等奖（金奖）30分/项；国家级一等奖20分/项；
        国家级二等奖15分/项；国家级三等奖10分/项
```

---

## 3. 16 行真实夹具（逐行断言）

以下输出是**每项带限定词**的最终期望值。测试必须逐字断言。

| # | 页 | 项目/内容 | 等级格 | 学分格 | 展开后 |
|---|---|---|---|---|---|
| 1 | p8 | 入选思想政治类宣讲团 | 区级、校级、院级 | `15、10、5分/次` | `区级15分/次；校级10分/次；院级5分/次` |
| 2 | p8 | 参与大学生讲思政课竞赛、大学生职业规划大赛等思想政治类比赛 | 国家级、区级、校级、院级 | `30、20、10、5分/次` | `国家级30分/次；区级20分/次；校级10分/次；院级5分/次` |
| 3 | p9 | 获国家级奖项（挑战杯 A 类） | 特等奖（金奖）、一、二、三等奖 | `30、20、15、10分/项` | `国家级特等奖（金奖）30分/项；国家级一等奖20分/项；国家级二等奖15分/项；国家级三等奖10分/项` |
| 4 | p9 | 获区级奖项（挑战杯 A 类） | 特等奖（金奖）、一、二、三等奖 | `15、10、8、5分/项` | `区级特等奖（金奖）15分/项；区级一等奖10分/项；区级二等奖8分/项；区级三等奖5分/项` |
| 5 | p9 | 获校级奖项（挑战杯 A 类） | 特等奖（金奖）、一、二、三等奖 | `8、5、3、2分/项` | `校级特等奖（金奖）8分/项；校级一等奖5分/项；校级二等奖3分/项；校级三等奖2分/项` |
| 6 | p9 | 获国家级奖项（学科竞赛 B/C/D） | 一、二、三等奖 | `15、10、8分/项` | `国家级一等奖15分/项；国家级二等奖10分/项；国家级三等奖8分/项` |
| 7 | p9 | 获区级奖项（学科竞赛 B/C/D） | 一、二、三等奖 | `8、5、3分/项` | `区级一等奖8分/项；区级二等奖5分/项；区级三等奖3分/项` |
| 8 | p9 | 获国家级奖项（非目录学科竞赛） | 一、二、三等奖 | `10、8、5分/项` | `国家级一等奖10分/项；国家级二等奖8分/项；国家级三等奖5分/项` |
| 9 | p9 | 获区级奖项（非目录学科竞赛） | 一、二、三等奖 | `5、3、2分/项` | `区级一等奖5分/项；区级二等奖3分/项；区级三等奖2分/项` |
| 10 | p10 | 文体竞赛·获国家级奖项一、二、三等奖 | 其他（真实等级在左邻 2 格） | `18、15、12分/项` | `国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项` |
| 11 | p10 | 文体竞赛·获区级奖项一、二、三等奖 | 其他 | `12、10、8分/项` | `区级一等奖12分/项；区级二等奖10分/项；区级三等奖8分/项` |
| 12 | p10 | 文体竞赛·获校级奖项一、二、三等奖 | 其他 | `8、6、4分/项` | `校级一等奖8分/项；校级二等奖6分/项；校级三等奖4分/项` |
| 13 | p10 | 文体竞赛·获院级奖项一、二、三等奖 | 其他 | `4、3、2分/项` | `院级一等奖4分/项；院级二等奖3分/项；院级三等奖2分/项` |
| 14 | p11 | 参加志愿服务项目大赛·获国家级奖项 | 金奖、银奖、铜奖 | `30、20、15分/项` | `国家级金奖30分/项；国家级银奖20分/项；国家级铜奖15分/项` |
| 15 | p11 | 参加志愿服务项目大赛·获区级奖项 | 金奖、银奖、铜奖 | `15、10、8分/项` | `区级金奖15分/项；区级银奖10分/项；区级铜奖8分/项` |
| 16 | p11 | 参加志愿服务项目大赛·获校级奖项 | 金奖、银奖、铜奖 | `8、5、3分/项` | `校级金奖8分/项；校级银奖5分/项；校级铜奖3分/项` |

> 实测：原方案不带限定词时 16/16 可展开；带限定词后最大长度比 4.3 倍，仍全部可展开。`特等奖（金奖）` 保留括号；p10 的 `其他` 不进入输出。

---

## 4. 任务

### Task 1: 纯规则模块 `table_rules.py`

**Files:**
- Create: `backend/app/ingestion/table_rules.py`
- Create: `backend/tests/unit/test_table_rules_multi.py`

**Interfaces:**
- `expand_multi_value_row(raw_row: list[str], col_headers: list[str | None]) -> dict[int, str]`
  - 返回 `{}` 或 `{学分列下标: 展开文本}`。
  - 只看 `raw_row`，不看 `forward_fill` 后的行。
- 内部函数：
  - `_split_items(cell) -> list[str]`
  - `_is_award_item(item) -> bool`
  - `_is_scope_item(item) -> bool`
  - `_clean_grade_item(item) -> str`
  - `_find_scope(*cells) -> str`
  - `_credit_values(cell) -> tuple[list[str], str] | None`（值列表 + 后缀）
- `grade_cell_index(raw_row, credit_idx) -> int | None`
  - 找左邻 1/2 的等级格，返回下标；找不到返回 `None`。
  - `expand_multi_value_row` 内部复用它；渲染器用它判断等级格是否就是“课程内容”列（p8/p10 是，p9/p11 不是）。

- [ ] **Step 1: 写失败测试（16 行夹具 + 反例）**

```python
import pytest
from app.ingestion.table_rules import expand_multi_value_row

P8_H = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]
P9_H = ["课程类别", "课程项目", "课程内容", None, "参考学分", "开课单位", "获得学分方式"]

CASES = [
    # p8：6 列，等级本身是范围项
    (P8_H, ["", "入选思想政治类宣讲团", "区级、校级、院级",
            "15、10、5分/次", "团委、各学院团委", "方式"], 3,
     "区级15分/次；校级10分/次；院级5分/次"),
    (P8_H, ["", "参与大学生讲思政课竞赛、大学生职业规划大赛等思想政治类比赛",
            "国家级、区级、校级、院级", "30、20、10、5分/次", "学工部（处）、团委、各学院团委", "方式"], 3,
     "国家级30分/次；区级20分/次；校级10分/次；院级5分/次"),

    # p9：7 列，范围词在内容格
    (P9_H, ["", "“挑战杯”“国创赛”…A类竞赛", "获国家级奖项",
            "特等奖（金奖）、一、二、三等奖", "30、20、15、10分/项", "各学院团委", "方式"], 4,
     "国家级特等奖（金奖）30分/项；国家级一等奖20分/项；国家级二等奖15分/项；国家级三等奖10分/项"),
    (P9_H, ["", "“挑战杯”“国创赛”…A类竞赛", "获区级奖项",
            "特等奖（金奖）、一、二、三等奖", "15、10、8、5分/项", "各学院团委", "方式"], 4,
     "区级特等奖（金奖）15分/项；区级一等奖10分/项；区级二等奖8分/项；区级三等奖5分/项"),
    (P9_H, ["", "“挑战杯”“国创赛”…A类竞赛", "获校级奖项",
            "特等奖（金奖）、一、二、三等奖", "8、5、3、2分/项", "各学院团委", "方式"], 4,
     "校级特等奖（金奖）8分/项；校级一等奖5分/项；校级二等奖3分/项；校级三等奖2分/项"),
    (P9_H, ["", "学科竞赛（B/C/D）", "获国家级奖项",
            "一、二、三等奖", "15、10、8分/项", "各学院团委", "方式"], 4,
     "国家级一等奖15分/项；国家级二等奖10分/项；国家级三等奖8分/项"),
    (P9_H, ["", "学科竞赛（B/C/D）", "获区级奖项",
            "一、二、三等奖", "8、5、3分/项", "各学院团委", "方式"], 4,
     "区级一等奖8分/项；区级二等奖5分/项；区级三等奖3分/项"),
    (P9_H, ["", "学科竞赛（非目录）", "获国家级奖项",
            "一、二、三等奖", "10、8、5分/项", "各学院团委", "方式"], 4,
     "国家级一等奖10分/项；国家级二等奖8分/项；国家级三等奖5分/项"),
    (P9_H, ["", "学科竞赛（非目录）", "获区级奖项",
            "一、二、三等奖", "5、3、2分/项", "各学院团委", "方式"], 4,
     "区级一等奖5分/项；区级二等奖3分/项；区级三等奖2分/项"),

    # p10：7 列，左邻 1 是“其他”，真实等级在左邻 2
    (P9_H, ["", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
            "18、15、12分/项", "团委、体育部、各学院", "方式"], 4,
     "国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项"),
    (P9_H, ["", "文体竞赛", "获区级奖项一、二、三等奖", "其他",
            "12、10、8分/项", "团委、体育部、各学院", "方式"], 4,
     "区级一等奖12分/项；区级二等奖10分/项；区级三等奖8分/项"),
    (P9_H, ["", "文体竞赛", "获校级奖项一、二、三等奖", "其他",
            "8、6、4分/项", "团委、体育部、各学院", "方式"], 4,
     "校级一等奖8分/项；校级二等奖6分/项；校级三等奖4分/项"),
    (P9_H, ["", "文体竞赛", "获院级奖项一、二、三等奖", "其他",
            "4、3、2分/项", "团委、体育部、各学院", "方式"], 4,
     "院级一等奖4分/项；院级二等奖3分/项；院级三等奖2分/项"),

    # p11：7 列，范围词在内容格
    (P9_H, ["", "参加志愿服务项目大赛", "获国家级奖项",
            "金奖、银奖、铜奖", "30、20、15分/项", "团委", "方式"], 4,
     "国家级金奖30分/项；国家级银奖20分/项；国家级铜奖15分/项"),
    (P9_H, ["", "参加志愿服务项目大赛", "获区级奖项",
            "金奖、银奖、铜奖", "15、10、8分/项", "团委", "方式"], 4,
     "区级金奖15分/项；区级银奖10分/项；区级铜奖8分/项"),
    (P9_H, ["", "参加志愿服务项目大赛", "获校级奖项",
            "金奖、银奖、铜奖", "8、5、3分/项", "团委", "方式"], 4,
     "校级金奖8分/项；校级银奖5分/项；校级铜奖3分/项"),
]


@pytest.mark.parametrize("headers,row,credit_idx,expected", CASES)
def test_expand_fixture(headers, row, credit_idx, expected):
    assert expand_multi_value_row(row, headers) == {credit_idx: expected}
```

反例：

```python
def test_single_value_not_expanded():
    row = ["技能特长", "语言类证书", "CET6", "大学英语六级", "5分/项", "各学院", "方式"]
    assert expand_multi_value_row(row, P9_H) == {}


def test_non_grade_multi_value_not_expanded():
    row = ["实践实习", "学生干部", "内容",
           "各校级学生组织副职、各学生社团副职", "15、10分/项", "各学院", "方式"]
    assert expand_multi_value_row(row, P9_H) == {}


def test_item_count_mismatch_not_expanded():
    row = ["", "项目", "内容", "一、二、三等奖", "15、10分/项", "各学院", "方式"]
    assert expand_multi_value_row(row, P9_H) == {}


def test_mixed_unit_not_expanded():
    row = ["", "项目", "内容", "一、二等奖", "15分/项、10分/次", "各学院", "方式"]
    assert expand_multi_value_row(row, P9_H) == {}


def test_other_is_not_grade_but_left2_is():
    row = ["", "文体竞赛", "获国家级奖项一、二、三等奖", "其他", "18、15、12分/项", "", ""]
    assert expand_multi_value_row(row, P9_H) == {
        4: "国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项"
    }
```

- [ ] **Step 2: 跑失败**

```bash
cd backend
uv run pytest tests/unit/test_table_rules_multi.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

import re

GRADE_TOKEN_RE = re.compile(
    r"(?:特等奖|金奖|银奖|铜奖|优胜奖|[一二三四五](?:等奖)?)$"
)
SCOPE_RE = re.compile(r"(国家级|区级|校级|院级)")
SCOPE_ITEM_RE = re.compile(r"^(?:国家级|区级|校级|院级)$")
CREDIT_RE = re.compile(r"^[\d、.\s]+分[/／]\S*$")
MAX_EXPANSION_RATIO = 5.0


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
    for off in (1, 2):
        gi = credit_idx - off
        if gi < 0:
            break
        cell = raw_row[gi].strip() if gi < len(raw_row) else ""
        items = _split_items(cell)
        if items and all(_is_award_item(x) or _is_scope_item(x) for x in items):
            return gi
    return None


def expand_multi_value_row(raw_row: list[str], col_headers: list[str | None]) -> dict[int, str]:
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

    out_items = []
    for item, value in zip(items, values):
        cleaned = _clean_grade_item(item)
        if scope and not cleaned.startswith(scope):
            cleaned = scope + cleaned
        out_items.append(cleaned + value)
    expanded = "；".join(out_items)
    if len(expanded) > len(cell) * MAX_EXPANSION_RATIO:
        return {}
    return {credit_idx: expanded}
```

- [ ] **Step 4: 跑测试**

16 行逐字通过 + 反例通过。

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/table_rules.py backend/tests/unit/test_table_rules_multi.py
git commit -m "[feat] 增加多值学分展开规则"
```

---

### Task 2: 接进自然句渲染器

**Files:**
- Modify: `backend/app/ingestion/table_render.py`
- Test: `backend/tests/unit/test_table_render.py`

**Interfaces:**
- `_render_doc12` 内部调用 `expand_multi_value_row(raw_row, col_headers)`；
- 如果 `expanded_text` 存在：
  - 用 `grade_cell_index` 找等级格；如果等级格就是“课程内容”列（p8/p10），**用展开文本替换内容**，避免重复；
  - 否则（p9/p11）保留内容，展开文本接在内容后面；
  - 句子 = `{类别} - {项目}[- {内容}]：{expanded_text}` + 可选 `，{单位}` + 可选 `（{方式}）` + `。`
  - **不再**追加“参考学分”，也不追加未命名等级格；
  - 展开文本已经包含限定词和分值。

- [ ] **Step 1: 写失败测试**

```python
P8_H = ["课程类别", "课程项目", "课程内容", "参考学分", "开课单位", "获得学分方式"]


def test_render_doc12_multi_value_p10():
    raw = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "",
           "18、15、12分/项", "团委、体育部、各学院",
           "由活动主办单位通过系统统一导入赋予学分"]
    filled = ["文化艺术与体育", "文体竞赛", "获国家级奖项一、二、三等奖", "其他",
              "18、15、12分/项", "团委、体育部、各学院",
              "由活动主办单位通过系统统一导入赋予学分"]
    assert render_sentence(raw, filled, DOC12_H) == (
        "文化艺术与体育 - 文体竞赛："
        "国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项，"
        "团委、体育部、各学院（由活动主办单位通过系统统一导入赋予学分）。"
    )


def test_render_doc12_multi_value_p8():
    raw = ["", "入选思想政治类宣讲团", "区级、校级、院级",
           "15、10、5分/次", "团委、各学院团委", "方式"]
    assert render_sentence(raw, raw, P8_H) == (
        "入选思想政治类宣讲团：区级15分/次；校级10分/次；院级5分/次，"
        "团委、各学院团委（方式）。"
    )


def test_render_doc12_multi_value_p9():
    raw = ["科技学术与创新创业", "“挑战杯”…A类竞赛", "获国家级奖项",
           "特等奖（金奖）、一、二、三等奖", "30、20、15、10分/项", "各学院团委",
           "学生自主申请，由学院团委统一认定"]
    sentence = render_sentence(raw, raw, DOC12_H)
    assert "国家级特等奖（金奖）30分/项" in sentence
    assert "参考学分" not in sentence, "展开后不得重复分值字段"
    assert "特等奖（金奖）、一、二、三等奖" not in sentence, "展开后不得重复等级格"
```

- [ ] **Step 2: 跑失败**

```bash
cd backend
uv run pytest tests/unit/test_table_render.py -k multi_value -v
```

- [ ] **Step 3: 修改 `_render_doc12`**

```python
from app.ingestion.table_rules import expand_multi_value_row, grade_cell_index

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

    expanded = expand_multi_value_row(raw_row, col_headers)
    score_idx = col_headers.index("参考学分") if "参考学分" in col_headers else -1
    expanded_text = _escape(expanded.get(score_idx, "")) if score_idx >= 0 else ""
    grade_idx = grade_cell_index(raw_row, score_idx) if expanded_text else None
    content_idx = col_headers.index("课程内容") if "课程内容" in col_headers else -1
    content_for_sentence = "" if grade_idx == content_idx else content

    head = " - ".join(p for p in (cat, proj) if p)
    if content_for_sentence:
        if detail_idx is None:
            head = f"{head}：{content_for_sentence}" if head else content_for_sentence
        else:
            head = f"{head} - {content_for_sentence}" if head else content_for_sentence
    if expanded_text:
        head += f"：{expanded_text}"
    else:
        if detail and detail not in PLACEHOLDERS:
            head += f"：{detail}"
        if score:
            head += f"，参考学分 {score}"
    if unit:
        head += f"，{unit}"
    if way:
        head += f"（{way}）"
    return head + "。" if head else ""
```

- [ ] **Step 4: 跑测试**

```bash
cd backend
uv run pytest tests/unit/test_table_render.py -v
```

⚠️ 方案乙的 `test_render_uses_raw_value_for_unnamed_column` 用了一个多值行；本任务后它会走展开路径，内容会被替换。把该测试的学分格改成单值（例如 `15分/个`），继续验证“未命名列用原始值、不把填充的 `其他` 写进句子”。

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/table_render.py backend/tests/unit/test_table_render.py
git commit -m "[feat] 自然句渲染接入多值学分展开"
```

---

### Task 3: 回归、长度与 A/B

- [ ] **Step 1: 跑全量表格测试**

```bash
cd backend
uv run pytest tests/unit/test_tables.py tests/unit/test_table_render.py tests/unit/test_table_rules_multi.py tests/integration/test_loaders.py -v
```

- [ ] **Step 2: 长度/组检查**

对 doc 12 生成自然句，检查：

- 16 行全部展开；
- 最长展开文本 ≤ 原格 5 倍；
- 没有一行的自然句超过 `budget` 太多（单行超预算仍自成一组是既有行为）；
- 总字数增幅 ≤15%。

- [ ] **Step 3: 离线 A/B**

用方案乙的同一套候选和两种后端，对比：

- A：不带多值展开的自然句；
- B：带多值展开的自然句。

重点看：

- 17 题口语集里 `tq-12`（文体竞赛国家级二等奖）、`tq-15`（挑战杯）目标块排名；
- 10 题专项集里 `sc-06`、`sc-07` 目标块排名；
- 不得让同表其他行的目标块变差超过 2 名。

- [ ] **Step 4: 记录并提交**

把结果写进本文档 §5；提交：

```bash
git add docs/superpowers/plans/2026-10-16-multi-value-expansion.md
git commit -m "[docs] 记录多值展开的离线 A/B 结果"
```

---

## 5. 验证门禁

**2026-10-18 实施记录：** 16/16 夹具通过；带限定词后最长 4.3 倍（5x 阀内）；真实 doc 12 p10 已输出 `文体竞赛：国家级一等奖18分/项；国家级二等奖15分/项；国家级三等奖12分/项`，原始 `18、15、12分/项` 不再出现。旧渲染 vs 新渲染的 rerank A/B 合并进用户要求的两轮 bge/jina 对比评测（施工后执行）。

| # | 检查 | 通过线 |
|---|---|---|
| 1 | 16 行夹具 | 16/16 与 §3 逐字一致 |
| 2 | 单值行 | 82 行零改动 |
| 3 | 非等级多值 / 项数不等 / 单位混用 | 零展开 |
| 4 | p8 范围项 | 不重复加前缀 |
| 5 | p9/p11 范围词在内容格 | 每项带正确前缀 |
| 6 | p10 左邻 2 格 | 正确展开，`其他` 不进入输出 |
| 7 | 自然句 | 展开行不再出现“参考学分”和原等级格 |
| 8 | 长度 | 最大比例 ≤5；总字数增幅 ≤15% |
| 9 | 离线 A/B | 目标行不差于展开前；不拖累同表其他行 |
| 10 | 既有测试 | 全绿 |

---

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 未来出现新等级写法（`A/B/C 档`） | 漏展开 | 原样保留，不猜 |
| 范围词出现在意料外的格子 | 前缀错/漏 | 只从等级格和其左一格找；找不到就不加前缀，仍展开 |
| 项数不等 | 漏展开 | 硬条件，不猜 |
| 文本变长 | 组数/字数变化 | 5 倍阀 + 组预算；A/B 门禁 |
| 与方案乙的模板冲突 | 重复/丢字段 | 展开路径独立分支，测试钉住“不重复参考学分/等级格” |
