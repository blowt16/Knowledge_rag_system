"""分块（§3.4.1 / §3.4.2）。

参数：500 字 / 50 重叠 / 中文标点切分 / 最小 5 字。

⚠️ `chunk_id` 的生成规则（§3.3.2）：
       chunk_id = f"{document_id}:{chunk_index}"
   它必须**稳定且全局唯一** —— 四处在用：
   `Citation.chunk_id`、RRF 融合去重（去重键就是它）、
   `qa_logs.retrieved_chunk_ids` / `reranked_chunk_ids`、`eval_cases.expected_chunk_ids`。

⚠️ PPTX 的 chunk **不跨幻灯片**（§3.4.2）：若单页文本超过 500 字阈值，页内再按标点切，
   **但不把两页的内容合进一个 chunk**，否则 `page` 无法表达。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import cfg

# 中文断句标点（含换行）—— 切分优先在这里断
_SENTENCE_END = "。！？；\n"
_SECONDARY_END = "，、：)）】」』"


@dataclass
class ChunkSpan:
    chunk_index: int
    char_start: int      # 相对规范化文本，Unicode 码点
    char_end: int
    text: str


def make_chunk_id(document_id: str, chunk_index: int) -> str:
    """chunk_id 的唯一生成点 —— 不要在各处自己拼。"""
    return f"{document_id}:{chunk_index}"


def chunk_size_hint() -> int:
    """表格分组的预算 —— 与 chunk_size 同源，避免两处各写一个数。"""
    return int(cfg("chunking.chunk_size", 500))


def _best_break(text: str, start: int, limit: int, *, floor: int) -> int:
    """在 [floor, limit] 区间里找一个尽量靠后的断句点。

    先找句末标点，找不到再退到次级标点，都不行就硬切在 limit。
    """
    window_end = min(limit, len(text))
    if window_end <= floor:
        return window_end

    best = -1
    for i in range(window_end - 1, floor - 1, -1):
        if text[i] in _SENTENCE_END:
            best = i + 1
            break
    if best != -1:
        return best

    for i in range(window_end - 1, floor - 1, -1):
        if text[i] in _SECONDARY_END:
            best = i + 1
            break
    return best if best != -1 else window_end


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

    # 原子区间：丢掉越界/退化的、按起点排序 —— 调用方传错不该让整篇入库失败
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
            # 撞上 boundary（或原子区间起点）：从这里重新开始，不重叠
            pos = end
            continue

        # 正常推进：回退 overlap 形成重叠
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = end
        pos = next_pos

    return chunks


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """按句末标点切句，返回 (start, end, sentence)。

    供 `cite` 节点的结论句校验与 5.2 的「无依据结论占比」使用。
    偏移相对传入的文本（对答案文本而言就是渲染前的原始答案）。
    """
    out: list[tuple[int, int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in _SENTENCE_END:
            end = i + 1
            piece = text[start:end]
            if piece.strip():
                out.append((start, end, piece))
            start = end
    if start < len(text):
        piece = text[start:]
        if piece.strip():
            out.append((start, len(text), piece))
    return out
