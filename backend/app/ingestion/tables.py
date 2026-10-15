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
