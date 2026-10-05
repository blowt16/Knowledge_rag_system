"""节点 10：cite —— 引用计算与声明级校验（§3.5.3 节点 10）。

**一次解析，产出两样东西**：`citations` + `verify_report`。
两者都在解析答案里的 `[n]` 标记，且「标记越界」在映射引用时必然被发现 ——
拆成两个节点等于把同一份解析做两遍。

⚠️ **越界判定的上界是 `len(evidence)`，不是 `len(candidates)`**：
   `candidates` 是 RRF 融合后的全量（可达 40 条），拿它做上界会让
   「只有 5 条证据却写了 `[7]`」这类真越界**漏检**。

⚠️ **不做的事**（明确留待后续）：
   ❌ 语义蕴含判定（引用片段是否**真的**支撑该结论）—— 需要 LLM 或 NLI 模型
   ❌ 自动删除无依据句子 —— 见下方「流式输出与后校验冲突」
   ❌ query 分解 + 逐子问题可回答性判定

**关键约束：流式输出与后校验冲突** —— token 一旦流出就已到达用户，
校验**无法撤回内容**。因此定位是：**标注，而非拦截**；**度量，而非纠正**。
"""

from __future__ import annotations

import re
import time

from app.graph.nodes.generate import is_conclusion_sentence
from app.graph.state import Chunk, RAGState
from app.schemas.chat import (
    Box,
    Citation,
    InvalidMarker,
    JumpTarget,
    UncitedClaim,
    VerifyReport,
)

_MARKER_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_RE = re.compile(r"[^。！？\n]*[。！？\n]?")


def _to_boxes(chunk: Chunk) -> list[Box]:
    boxes: list[Box] = []
    for raw in chunk.bbox or []:
        try:
            boxes.append(Box(
                page=int(raw.get("page", chunk.page)),
                x0=float(raw.get("x0", 0.0)),
                x1=float(raw.get("x1", 0.0)),
                top=float(raw.get("top", 0.0)),
                bottom=float(raw.get("bottom", 0.0)),
            ))
        except (TypeError, ValueError):
            continue
    return boxes


def build_citations(answer: str, evidence: list[Chunk]) -> list[Citation]:
    """按标记映射 chunk。只输出**答案中实际引用**的 chunk，而非全部检索结果。"""
    citations: list[Citation] = []
    seen: set[int] = set()

    for match in _MARKER_RE.finditer(answer or ""):
        marker = int(match.group(1))
        if marker in seen or not (1 <= marker <= len(evidence)):
            continue
        seen.add(marker)
        chunk = evidence[marker - 1]
        citations.append(Citation(
            marker=marker,
            document_name=chunk.document_name or chunk.document_id,
            chapter=chunk.chapter,
            page=chunk.page,
            snippet=chunk.text[:200],
            chunk_id=chunk.chunk_id,
            # 越权取得 —— 5.3 的 ACL 对照实验直接读它
            escalated=bool(chunk.escalated),
            # ⚠️ 存**文件名**不是签名 URL（签名 URL 5 分钟过期，citations 会落库）
            images=list(chunk.images or []),
            jump_target=JumpTarget(
                document_id=chunk.document_id,
                page=chunk.page,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                boxes=_to_boxes(chunk),
            ),
        ))
    citations.sort(key=lambda c: c.marker)
    return citations


def build_verify_report(answer: str, evidence: list[Chunk]) -> VerifyReport:
    """声明级校验。

    偏移相对**渲染前的原始答案文本**，单位 **Unicode 码点**
    （Python 的 len 就是码点语义）。
    """
    answer = answer or ""
    upper = len(evidence)

    invalid: list[InvalidMarker] = []
    for match in _MARKER_RE.finditer(answer):
        marker = int(match.group(1))
        if marker < 1 or marker > upper:
            invalid.append(InvalidMarker(marker=marker,
                                         char_start=match.start(),
                                         char_end=match.end()))

    total = 0
    cited = 0
    uncited: list[UncitedClaim] = []

    for match in _SENTENCE_RE.finditer(answer):
        raw = match.group(0)
        sentence = raw.strip()
        if not sentence:
            continue
        # 「结论句」定义与生成共用同一套；判定保守，拿不准的不算
        if not is_conclusion_sentence(sentence):
            continue
        total += 1

        markers = [int(m) for m in _MARKER_RE.findall(sentence)]
        # 只有**合法**标记才算「有依据」
        if any(1 <= m <= upper for m in markers):
            cited += 1
            continue

        # 带定位信息 —— 前端要把对应句子置灰。
        # 答案经 Markdown 渲染后**无法靠字符串查找可靠定位**（重复句子、
        # 渲染后文本变化），所以必须给偏移。
        leading = len(raw) - len(raw.lstrip())
        start = match.start() + leading
        uncited.append(UncitedClaim(
            sentence=sentence,
            char_start=start,
            char_end=start + len(sentence),
        ))

    return VerifyReport(
        total_claims=total,
        cited_claims=cited,
        invalid_markers=invalid,
        uncited_claims=uncited,
    )


async def cite_node(state: RAGState) -> dict:
    from app.graph.state import NodeTrace

    started = time.perf_counter()
    answer = state.get("answer") or ""
    evidence: list[Chunk] = state.get("evidence") or []

    def _trace(recalled: int):
        return [NodeTrace(node="cite",
                          ms=int((time.perf_counter() - started) * 1000),
                          recalled=recalled, degraded=None)]

    if state.get("decision") == "REFUSED_NO_EVIDENCE":
        # 拒答路径不发 citations / verify
        return {"citations": [], "verify_report": VerifyReport(),
                "trace": _trace(0)}

    citations = build_citations(answer, evidence)
    return {
        "citations": citations,
        "verify_report": build_verify_report(answer, evidence),
        "trace": _trace(len(citations)),
    }
