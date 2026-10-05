"""节点 8：build_context —— 上下文组装（§3.5.3 节点 8）。

```
reranked (Top-5)
      ↓
按文档分组 → 组内按 chunk_index 排序
      ↓
**裁剪（先裁后编号）**：超出预算 → 整块丢弃低分片段
      ↓
拼装：文档名 / 章节 / 页码 / 正文 / 图片占位
      ↓
编号 [1]…[N] → evidence（顺序即编号顺序）
      ↓
   context
```

**裁剪规格**（原方案只写「裁剪到 token 预算」六个字，四项都没定义）：

| 项 | 规格 |
|---|---|
| **单位** | **整块丢弃**，不做块内截断 —— 截断会让该 chunk 的 `char_start/char_end` 与 `bbox`（`jump_target` 的定位依据）指向残缺文本，**引用回跳会跳错** |
| **预算值** | **8,000 token**（与 §3.8.2 的检索上下文配额一致；`Top-5` 通常远达不到，此处是防御性上限） |
| **「低分」的定义** | 仍按 **rerank 分**（`reranked` 的顺序），**不是**重排后的文档分组顺序。分组只影响**展示顺序**，不影响裁谁 |
| **裁剪与编号的次序** | ⚠️ **先裁后编号** —— 否则裁掉中间某条会让编号出现空洞（有 `[1]` `[3]` 没有 `[2]`），而模型仍会照抄编号 |

⚠️ ★`evidence` 是「编号 → 证据」的唯一载体：`reranked` **不能替代它**，
   因为本节点是「按文档分组 → 组内按 chunk_index 排序」后才拼 context，
   **prompt 里的顺序 ≠ reranked 的顺序**。拿 reranked 做映射，
   `[1]` 会指向错的 chunk —— **引用错位比漏引用更糟**。
"""

from __future__ import annotations

import time

from app.core.config import cfg
from app.graph.state import Chunk, NodeTrace, RAGState

# token 计数用 dashscope 自带分词器（离线可用、零新增依赖）
_tokenizer = None


def count_tokens(text: str) -> int:
    global _tokenizer
    if _tokenizer is None:
        try:
            from dashscope.tokenizers import get_tokenizer
            _tokenizer = get_tokenizer(cfg("llm.tokenizer_model", "qwen3-max"))
        except Exception:  # noqa: BLE001 —— 拿不到分词器时退到保守估算
            _tokenizer = False
    if _tokenizer:
        try:
            return len(_tokenizer.encode(text))
        except Exception:  # noqa: BLE001
            pass
    # 保守估算：中文约 0.7 token/汉字（文档实测 1.72 字符/token）
    return int(len(text) * 0.7)


def build_evidence(chunks: list[Chunk]) -> tuple[str, list[Chunk]]:
    """裁剪 → 分组排序 → 编号 → 拼 context。返回 (context, evidence)。"""
    budget = int(cfg("context.retrieval_context_budget", 8000))

    # ① 裁剪：仍按 rerank 分（原始顺序），整块丢弃
    kept: list[Chunk] = []
    used = 0
    for chunk in chunks:
        cost = count_tokens(chunk.text) + 40      # 头部信息的粗略开销
        if kept and used + cost > budget:
            break
        kept.append(chunk)
        used += cost

    if not kept:
        return "", []

    # ② 展示顺序：按文档分组 → 组内按 chunk_index 排序
    grouped: dict[str, list[Chunk]] = {}
    for chunk in kept:
        grouped.setdefault(chunk.document_id or chunk.chunk_id, []).append(chunk)
    for group in grouped.values():
        group.sort(key=lambda c: c.chunk_index)

    # ③ 先裁后编号 —— 编号在裁剪之后分配，避免出现 [1] [3] 的空洞
    evidence: list[Chunk] = []
    for group in grouped.values():
        evidence.extend(group)

    lines: list[str] = []
    for index, chunk in enumerate(evidence, start=1):
        header = f"[{index}] 《{chunk.document_name or '未知文档'}》"
        locator = []
        if chunk.chapter:
            locator.append(chunk.chapter)
        locator.append(f"第 {chunk.page} 页")
        if locator:
            header += " · " + " · ".join(locator)
        lines.append(header)
        lines.append(chunk.text)
        if chunk.images:
            # 图片只记占位，描述在 E.4.5 已删除
            lines.append(f"（本段关联图片：{'、'.join(chunk.images)}）")
        lines.append("")

    return "\n".join(lines), evidence


async def build_context_node(state: RAGState) -> dict:
    started = time.perf_counter()
    context, evidence = build_evidence(state.get("reranked") or [])

    return {
        "context": context,
        # ★ 顺序即 prompt 里的 [1]…[N]
        "evidence": evidence,
        "trace": [NodeTrace(
            node="build_context",
            ms=int((time.perf_counter() - started) * 1000),
            recalled=len(evidence),
            degraded=None,
        )],
    }
