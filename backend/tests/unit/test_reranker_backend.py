"""精排后端开关（`reranker.backend`）。

为什么要单独一条：两个后端的**接口与分数刻度都不同**，切换时最容易犯的错是
「换了模型没换阈值」——bge 的 0.3 套到 Jina 上会把候选全砍光（2026-10-17 实测，
Jina 对相关块也只给 0.28–0.50）。那种故障表现为「系统突然什么都拒答」，
和模型坏了很难区分。这里锁住两件事：profile 的回退链、以及 listwise 分数要按下标还原。
"""
from __future__ import annotations

import pytest

from app.graph.state import Chunk
from app.retrieval import reranker


def _cfg_with(overrides: dict):
    """构造一个**严格**的假 cfg：只认 overrides，其余一律返回 default。

    ⚠️ 不能回退真实配置：app.yaml 里 `jina_listwise` 块本来就写了 min_score，
       一回退就把测试想验证的「profile 没写 → 走共用键」这条路径盖住了
       （第一版这么写，测试直接挂了 —— 是测试的错，不是实现的错）。
    """
    def fake(path: str, default=None):
        return overrides.get(path, default)

    return fake


def test_backend_defaults_to_cross_encoder(monkeypatch):
    monkeypatch.setattr(reranker, "cfg", _cfg_with({}))
    assert reranker.backend() == "cross_encoder"


def test_pcfg_prefers_backend_profile(monkeypatch):
    """后端专属块优先于共用默认。"""
    monkeypatch.setattr(reranker, "cfg", _cfg_with({
        "reranker.backend": "jina_listwise",
        "reranker.jina_listwise.min_score": 0.1,
        "reranker.min_score": 0.3,
    }))
    assert reranker._pcfg("min_score", 0.3) == 0.1


def test_pcfg_falls_back_to_shared_key(monkeypatch):
    """profile 没写的项回退到共用默认。

    ⚠️ 这条回退是 cross_encoder 能用既有扁平键（`reranker.min_score`）的原因 ——
       `test_reranker_threshold.py` 正是 patch 那个键，回退链断了它就先挂。
    """
    monkeypatch.setattr(reranker, "cfg", _cfg_with({
        "reranker.backend": "jina_listwise",
        "reranker.min_score": 0.42,
    }))
    assert reranker._pcfg("min_score", 0.3) == 0.42


class _FakeListwise:
    """伪造 listwise 模型：故意打乱返回顺序，验证按下标还原。"""

    def rerank(self, query, documents):
        n = len(documents)
        return [{"document": documents[i], "relevance_score": float(i) / 10,
                 "index": i} for i in reversed(range(n))]


def test_listwise_scores_are_aligned_by_index(monkeypatch):
    monkeypatch.setattr(reranker, "cfg", _cfg_with({"reranker.backend": "jina_listwise"}))
    chunks = [Chunk(chunk_id=f"c{i}", document_id="d", chunk_index=i,
                    text=f"第{i}块", page=1) for i in range(3)]
    monkeypatch.setattr(reranker, "_model", _FakeListwise())

    scores = reranker._predict("随便问问", chunks)

    assert len(scores) == len(chunks)
    assert scores == pytest.approx([0.0, 0.1, 0.2]), "分数必须按 index 归位，不能按返回顺序"
