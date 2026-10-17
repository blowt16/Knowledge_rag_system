"""rerank 分数阈值过滤（§3.5.3 节点 7）：低于 `reranker.min_score` 的 chunk 直接丢弃。

实测依据（2026-10-14，eval_min20 夹具 294 个候选全量打分）：
    噪声 chunk 中位数 0.476、下四分位 0.101；期望命中 chunk 中位数 0.885。
    阈值 0.2–0.6 扫描 Recall@5 零损失，默认 0.3 裁在噪声分布的下 1/4 尾上。
"""

from __future__ import annotations

import pytest

from app.graph.state import Chunk
from app.retrieval import reranker


@pytest.fixture(autouse=True)
def _pin_cross_encoder(monkeypatch):
    """这些用例测的是 **cross_encoder** 后端，必须显式钉住。

    ⚠️ 为什么不能靠 app.yaml：`_pcfg` 的优先级是「后端专属 → 共用默认」，
       而配置现在可能切到 `jina_listwise`。不钉住的话：
         · 阈值用例 patch 的 `reranker.min_score` 会被 `jina_listwise.min_score`
           覆盖 —— 测试变成在测另一套配置，还看起来全绿；
         · 预热用例 mock 的 `CrossEncoder` 根本不会被调用（走的是 Jina 分支）。
       2026-10-17 切后端时正是这样挂了 8 条。
    """
    monkeypatch.setattr(reranker, "backend", lambda: "cross_encoder")



@pytest.fixture(autouse=True)
def _reset_model_state():
    """模型是模块级全局；用例必须自己清理，否则相互污染。"""
    reranker._model = None
    reranker._load_failed = False
    yield
    reranker._model = None
    reranker._load_failed = False


class _FakeModel:
    """一次调用 = 一批候选，按调用顺序返回预置分数。"""

    def __init__(self, scores: list[float]):
        self.scores = scores

    def predict(self, pairs, **kwargs):
        return self.scores


def _patch_scores(monkeypatch, scores: list[float], min_score: float) -> None:
    monkeypatch.setattr(
        "sentence_transformers.CrossEncoder", lambda *a, **k: _FakeModel(scores))
    real_cfg = reranker.cfg
    monkeypatch.setattr(
        reranker, "cfg",
        lambda path, default=None: min_score if path == "reranker.min_score"
        else real_cfg(path, default))


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", text=f"文本{i}") for i in range(n)]


def test_drops_chunks_below_min_score(monkeypatch):
    """低于阈值的 chunk 不进结果；保留部分仍按分数降序。"""
    chunks = _chunks(3)
    _patch_scores(monkeypatch, scores=[0.9, 0.2, 0.5], min_score=0.3)

    result = reranker.rerank_sync("查询", chunks, top_k=5)

    assert [c.chunk_id for c in result.chunks] == ["c0", "c2"]
    assert not result.degraded


def test_keeps_chunk_equal_to_threshold(monkeypatch):
    """等于阈值要保留 —— 过滤条件是严格小于。"""
    chunks = _chunks(2)
    _patch_scores(monkeypatch, scores=[0.3, 0.29], min_score=0.3)

    result = reranker.rerank_sync("查询", chunks, top_k=5)

    assert [c.chunk_id for c in result.chunks] == ["c0"]


def test_all_below_threshold_is_empty_not_degraded(monkeypatch):
    """全滤掉 = 空列表 + 非降级：节点层据「reranked 为空」走零成本拒答。

    ⚠️ 必须是 degraded=False —— 降级语义是「精排坏了，换 RRF 顺序兜底」，
       全滤掉是「精排正常工作，结论是都不相关」，两者不能混。
    """
    chunks = _chunks(2)
    _patch_scores(monkeypatch, scores=[0.1, 0.2], min_score=0.3)

    result = reranker.rerank_sync("查询", chunks, top_k=5)

    assert result.chunks == []
    assert result.degraded is False
    assert result.degraded_kind is None
    assert result.confidence == 0.2     # 置信度仍取全体最高分，不受阈值影响


def test_degraded_path_does_not_filter(monkeypatch):
    """降级（模型加载失败）时按 RRF 顺序返回，不做阈值过滤 —— 没有精排分可滤。"""
    chunks = _chunks(3)
    monkeypatch.setattr(
        "sentence_transformers.CrossEncoder",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    real_cfg = reranker.cfg
    monkeypatch.setattr(reranker, "cfg", lambda p, d=None:
                        0.3 if p == "reranker.min_score" else real_cfg(p, d))

    result = reranker.rerank_sync("查询", chunks, top_k=5)

    assert [c.chunk_id for c in result.chunks] == ["c0", "c1", "c2"]
    assert result.degraded is True
