"""图节点 span 的 input/output 摘要（Langfuse UI 的 Input/Output 面板）。

**只放摘要**：query 原文、chunk_id / 文档名 / 页码 / 分数、正文前
`PREVIEW_CHARS` 字。完整检索正文**不上传** —— 节点 state 里可能挂着几十条
片段，逐节点全量上传会让云端数据量翻倍。

脱敏钩子 `mask_otel_spans` 对这里产出的所有字符串同样生效（学号/手机号在
导出前打码）。新增字段时照旧只放「能定位问题的最小信息」，别把整个 state
塞进来。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.core.logging import hash_query
from app.graph.state import Chunk, RAGState

#: 单个 chunk 正文最多上传多少字。够看出「这一段讲什么」，又不会把全文带上云。
PREVIEW_CHARS = 80


def _preview(text: str) -> str:
    return (text or "")[:PREVIEW_CHARS]


def _chunk_view(chunk: Chunk) -> dict[str, Any]:
    """单条候选的摘要。`score` 是 rerank/融合后的最终分。"""
    return {
        "chunk_id": chunk.chunk_id,
        "document": chunk.document_name,
        "page": chunk.page,
        "score": round(float(chunk.score), 4),
        "preview": _preview(chunk.text),
    }


def _chunk_views(chunks: list[Chunk] | None) -> list[dict[str, Any]]:
    return [_chunk_view(chunk) for chunk in (chunks or [])]


def node_input(node: str, state: RAGState) -> dict[str, Any]:
    """节点执行前的输入摘要。"""
    query = state.get("resolved_query") or state.get("query", "")
    if node == "resolve":
        return {
            "query": state.get("query", ""),
            "history_messages": len(state.get("history") or []),
        }
    if node in ("route", "chat", "clarify", "rewrite"):
        return {"query": query}
    if node == "retrieve":
        return {"queries": [q.get("text", "")
                            for q in (state.get("retrieval_queries") or [])]}
    if node == "rerank":
        candidates = state.get("candidates") or []
        return {
            "query": query,
            "candidate_count": len(candidates),
            "candidates": _chunk_views(candidates),
        }
    if node == "build_context":
        reranked = state.get("reranked") or []
        return {
            "reranked_count": len(reranked),
            "reranked": _chunk_views(reranked),
        }
    if node == "generate":
        return {
            "query": query,
            "evidence_count": len(state.get("evidence") or []),
            "context_chars": len(state.get("context") or ""),
        }
    if node == "cite":
        return {
            "answer_chars": len(state.get("answer") or ""),
            "evidence_count": len(state.get("evidence") or []),
        }
    if node == "refuse":
        return {"candidate_count": len(state.get("candidates") or [])}
    return {}


def node_output(node: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """节点返回后的输出摘要。`result` 是节点返回的 state 增量。"""
    if node == "resolve":
        return {
            "resolved_query": result.get("resolved_query", ""),
            "skipped_reason": result.get("resolve_skipped_reason", ""),
            "unresolved": bool(result.get("resolve_unresolved")),
        }
    if node == "route":
        return {
            "route": result.get("route", ""),
            "route_source": result.get("route_source", ""),
            "clarify_skipped": bool(result.get("clarify_skipped")),
        }
    if node == "chat":
        answer = result.get("answer") or ""
        return {
            "decision": result.get("decision", ""),
            "answer_chars": len(answer),
            "answer_preview": _preview(answer),
        }
    if node == "clarify":
        return {
            "question": result.get("clarify_question", ""),
            "facets": list(result.get("clarify_facets") or []),
        }
    if node == "rewrite":
        return {
            "queries": [
                {"text": q.get("text", ""), "source": q.get("source", ""),
                 "target": q.get("target", "")}
                for q in (result.get("retrieval_queries") or [])
            ],
        }
    if node == "retrieve":
        candidates = result.get("candidates") or []
        return {
            "candidate_count": len(candidates),
            "candidates": _chunk_views(candidates),
            "degraded": bool(result.get("rerank_degraded")),
        }
    if node == "rerank":
        reranked = result.get("reranked") or []
        return {
            "reranked_count": len(reranked),
            "reranked": _chunk_views(reranked),
            "confidence": result.get("retrieval_confidence"),
            "degraded": bool(result.get("rerank_degraded")),
        }
    if node == "refuse":
        return {"refusal_reason": result.get("refusal_reason", "")}
    if node == "build_context":
        evidence = result.get("evidence") or []
        return {
            "evidence_count": len(evidence),
            "context_chars": len(result.get("context") or ""),
            "evidence": _chunk_views(evidence),
        }
    if node == "generate":
        answer = result.get("answer") or ""
        return {
            "decision": result.get("decision", ""),
            "answer_chars": len(answer),
            "answer_preview": _preview(answer),
        }
    if node == "cite":
        citations = result.get("citations") or []
        return {
            "citation_count": len(citations),
            "citations": [
                {"marker": c.marker, "chunk_id": c.chunk_id,
                 "document": c.document_name, "page": c.page}
                for c in citations
            ],
        }
    return {}


def node_log_fields(node: str, result: Mapping[str, Any],
                    state: Mapping[str, Any]) -> dict[str, Any]:
    """节点完成日志的标量字段 —— 绝不放 query / answer 正文。"""
    fields: dict[str, Any] = {}
    entries = result.get("trace") or []
    last = entries[-1] if entries else {}
    if last.get("degraded"):
        fields["degraded"] = str(last["degraded"])

    if node == "resolve":
        original = str(state.get("query", ""))
        resolved = str(result.get("resolved_query", ""))
        fields["query_len"] = len(original)
        fields["query_hash"] = hash_query(original)
        fields["changed"] = bool(resolved and resolved != original)
        fields["skipped"] = bool(result.get("resolve_skipped_reason"))
    elif node == "route":
        fields["route"] = result.get("route", "")
        fields["source"] = result.get("route_source", "")
        fields["clarify_skipped"] = bool(result.get("clarify_skipped"))
    elif node == "rewrite":
        fields["queries"] = len(result.get("retrieval_queries") or [])
    elif node == "retrieve":
        fields["candidates"] = len(result.get("candidates") or [])
    elif node == "rerank":
        reranked = result.get("reranked") or []
        fields["kept"] = len(reranked)
        fields["top"] = (round(float(getattr(reranked[0], "score", 0.0)), 4)
                         if reranked else 0.0)
        fields["confidence"] = result.get("retrieval_confidence")
    elif node == "build_context":
        fields["evidence"] = len(result.get("evidence") or [])
        fields["chars"] = len(result.get("context") or "")
    elif node == "generate":
        fields["decision"] = result.get("decision", "")
        fields["chars"] = len(result.get("answer") or "")
    elif node == "cite":
        fields["citations"] = len(result.get("citations") or [])
    elif node == "refuse":
        fields["reason"] = result.get("refusal_reason", "")
    elif node == "chat":
        fields["decision"] = result.get("decision", "")
        fields["chars"] = len(result.get("answer") or "")
    elif node == "clarify":
        fields["facets"] = len(result.get("clarify_facets") or [])
    return {k: v for k, v in fields.items() if v is not None}
