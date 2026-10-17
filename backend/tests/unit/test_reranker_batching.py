"""Jina listwise 分批精排（`reranker.jina_listwise.batch_candidates`）。

listwise 的 token 数随候选数线性增长，24 个候选在 6GB 显卡上会颠簸到
几十秒。分批把峰值显存压回安全区，但**分数必须按全局下标还原** ——
每批返回的 `index` 是批内下标，不是原始候选下标。
"""
from __future__ import annotations

import pytest

from app.graph.state import Chunk
from app.retrieval import reranker


def _cfg_with(overrides: dict):
    def fake(path: str, default=None):
        return overrides.get(path, default)
    return fake


class _RecordingListwise:
    """记录每批的文档；分数编码在文档文本里，便于验证全局下标映射。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def rerank(self, query, documents):
        self.calls.append(list(documents))
        rows = []
        for local_index, doc in enumerate(documents):
            global_index = int(doc.removeprefix("g"))
            rows.append({"document": doc,
                         "relevance_score": global_index / 100,
                         "index": local_index})
        return list(reversed(rows))   # 故意打乱返回顺序


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", text=f"g{i}") for i in range(n)]


def test_listwise_splits_into_configured_batches(monkeypatch):
    monkeypatch.setattr(reranker, "cfg", _cfg_with({
        "reranker.backend": "jina_listwise",
        "reranker.jina_listwise.batch_candidates": 2,
    }))
    fake = _RecordingListwise()
    monkeypatch.setattr(reranker, "_model", fake)

    scores = reranker._predict("查询", _chunks(5))

    assert [len(call) for call in fake.calls] == [2, 2, 1], "没有按 2 个一批切分"
    assert scores == pytest.approx([0.0, 0.01, 0.02, 0.03, 0.04]), \
        "分数必须按全局下标还原，不能把每批的下标当成原始下标"


def test_listwise_batch_zero_means_single_call(monkeypatch):
    monkeypatch.setattr(reranker, "cfg", _cfg_with({
        "reranker.backend": "jina_listwise",
        "reranker.jina_listwise.batch_candidates": 0,
    }))
    fake = _RecordingListwise()
    monkeypatch.setattr(reranker, "_model", fake)

    scores = reranker._predict("查询", _chunks(5))

    assert [len(call) for call in fake.calls] == [5], "0 应表示关闭分批"
    assert scores == pytest.approx([0.0, 0.01, 0.02, 0.03, 0.04])


def test_listwise_under_batch_limit_is_one_call(monkeypatch):
    monkeypatch.setattr(reranker, "cfg", _cfg_with({
        "reranker.backend": "jina_listwise",
        "reranker.jina_listwise.batch_candidates": 10,
    }))
    fake = _RecordingListwise()
    monkeypatch.setattr(reranker, "_model", fake)

    scores = reranker._predict("查询", _chunks(5))

    assert [len(call) for call in fake.calls] == [5]
    assert scores == pytest.approx([0.0, 0.01, 0.02, 0.03, 0.04])
