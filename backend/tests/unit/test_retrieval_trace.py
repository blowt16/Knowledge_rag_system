"""检索子阶段日志。"""
from __future__ import annotations

import logging

from app.core import trace
from app.graph.state import Chunk
from app.retrieval import embedding, fusion, reranker


def _chunk(cid: str, score: float = 0.0) -> Chunk:
    return Chunk(chunk_id=cid, text="t", score=score)


def test_rrf_logs_counts(caplog):
    with trace.begin_trace():
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            out = fusion.weighted_rrf([
                ({"text": "a", "target": "both", "weight": 1.0, "source": "x"},
                 [_chunk("c1"), _chunk("c2")]),
            ])

    assert [c.chunk_id for c in out] == ["c1", "c2"]
    rec = [r for r in caplog.records if getattr(r, "stage", "") == "retrieve.rrf"][-1]
    assert rec.inputs == 2
    assert rec.outputs == 2


async def test_rerank_logs_candidates_and_kept(monkeypatch, caplog):
    def fake_sync(query, chunks, *, top_k=None):
        return reranker.RerankResult(
            chunks=chunks[:1], confidence=0.9, degraded=False)

    monkeypatch.setattr(reranker, "rerank_sync", fake_sync)

    with trace.begin_trace():
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            result = await reranker.rerank("q", [_chunk("c1"), _chunk("c2")])

    assert len(result.chunks) == 1
    rec = [r for r in caplog.records if getattr(r, "stage", "") == "rerank"][-1]
    assert rec.candidates == 2
    assert rec.kept == 1
    assert rec.confidence == 0.9


async def test_embed_query_logs_dim(monkeypatch, caplog):
    async def fake_embed(texts):
        assert texts == ["q"]
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(embedding, "embed_texts", fake_embed)

    with trace.begin_trace():
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            vector = await embedding.embed_query("q")

    assert vector == [0.1, 0.2, 0.3]
    rec = [r for r in caplog.records
           if getattr(r, "stage", "") == "retrieve.embed_query"][-1]
    assert rec.dim == 3
