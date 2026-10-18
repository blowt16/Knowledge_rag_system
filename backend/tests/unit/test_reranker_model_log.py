"""rerank 模型加载日志：必须能看到后端和模型名。"""
from __future__ import annotations

import logging

from app.retrieval import reranker


def test_model_info_includes_backend_and_model_name(monkeypatch):
    monkeypatch.setattr(reranker, "backend", lambda: "jina_listwise")
    monkeypatch.setattr(
        reranker, "_model_dir", lambda: "models/jinaai/jina-reranker-v3.5")

    info = reranker.model_info()

    assert info["backend"] == "jina_listwise"
    assert info["model"] == "jina-reranker-v3.5"
    assert info["path"].endswith("jina-reranker-v3.5")


def test_load_model_logs_backend_and_model(monkeypatch, caplog):
    monkeypatch.setattr(reranker, "backend", lambda: "cross_encoder")
    monkeypatch.setattr(
        reranker, "_model_dir", lambda: "models/BAAI/bge-reranker-v2-m3")

    class _FakeModel:
        pass

    monkeypatch.setattr(
        "sentence_transformers.CrossEncoder", lambda *a, **k: _FakeModel())
    reranker._model = None
    reranker._load_failed = False
    try:
        with caplog.at_level(logging.INFO, logger="app.retrieval.reranker"):
            model = reranker._load_model()
        assert model is not None
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "cross_encoder" in m and "bge-reranker-v2-m3" in m
            for m in messages
        ), f"加载日志里没有后端/模型名：{messages}"
    finally:
        reranker._model = None
        reranker._load_failed = False
