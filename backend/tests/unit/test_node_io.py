"""节点 I/O 摘要的取值（Langfuse span 的 input/output 面板数据源）。

⚠️ 只测**摘要内容**：query、chunk_id/文档名/页码/分数、正文前 N 字。
   完整正文不上传 —— 那条承诺由 `test_langfuse_node_span.py` 的发出字节兜住。
"""
from __future__ import annotations

from app.graph.node_io import PREVIEW_CHARS, node_input, node_output
from app.graph.state import Chunk


def _chunk(chunk_id: str, text: str, score: float, **kw) -> Chunk:
    return Chunk(chunk_id=chunk_id, text=text, score=score, **kw)


def test_rerank_input_lists_candidates_with_scores():
    state = {
        "query": "转专业条件",
        "candidates": [
            _chunk("c1", "第一块正文", 0.8123, document_name="学生手册", page=3),
            _chunk("c2", "第二块正文", 0.4567, document_name="学籍管理办法", page=12),
        ],
    }

    io = node_input("rerank", state)

    assert io["query"] == "转专业条件"
    assert io["candidate_count"] == 2
    assert io["candidates"] == [
        {"chunk_id": "c1", "document": "学生手册", "page": 3,
         "score": 0.8123, "preview": "第一块正文"},
        {"chunk_id": "c2", "document": "学籍管理办法", "page": 12,
         "score": 0.4567, "preview": "第二块正文"},
    ]


def test_rerank_output_keeps_new_order_and_confidence():
    result = {
        "reranked": [
            _chunk("c2", "第二块正文", 0.91, document_name="学生手册", page=12),
            _chunk("c1", "第一块正文", 0.42, document_name="学籍管理办法", page=3),
        ],
        "retrieval_confidence": 0.91,
        "rerank_degraded": False,
    }

    io = node_output("rerank", result)

    assert [c["chunk_id"] for c in io["reranked"]] == ["c2", "c1"]
    assert [c["score"] for c in io["reranked"]] == [0.91, 0.42]
    assert io["confidence"] == 0.91
    assert io["degraded"] is False


def test_chunk_preview_is_capped_at_preview_chars():
    text = "头" * PREVIEW_CHARS + "尾标记"

    io = node_input("rerank", {"query": "q",
                               "candidates": [_chunk("c1", text, 0.5)]})

    preview = io["candidates"][0]["preview"]
    assert len(preview) == PREVIEW_CHARS
    assert "尾标记" not in preview


def test_generate_input_sends_context_length_not_text():
    state = {"query": "q", "evidence": [_chunk("c1", "正文", 0.9)],
             "context": "很长的上下文正文" * 100}

    io = node_input("generate", state)

    assert io["context_chars"] == len("很长的上下文正文" * 100)
    assert "context" not in io
