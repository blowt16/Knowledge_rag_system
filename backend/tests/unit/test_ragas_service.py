"""ragas 服务的护栏（不联网、不起真子进程）。

真跑 ragas 要调 LLM 判官（一条样本约 20 秒），不适合放进快速测试套件 ——
**真链路的手工验证方式见 `docs/评测与ragas.md`**：
    cd backend && uv run python -m app.services.ragas_service

这里锁的是三件事：
1. 隔离环境不在时**不抛异常**，而是返回 `available: False`（与 stats/retrieval 同口径）
2. 空样本不浪费一次子进程
3. 解释器路径的解析规则（`RAGAS_PYTHON` 覆盖 + 平台差异）
"""

from __future__ import annotations

import pytest

from app.services import ragas_service


def test_venv_python_honours_override(monkeypatch, tmp_path):
    """RAGAS_PYTHON 指到哪儿就用哪儿；指向不存在的路径返回 None。"""
    fake = tmp_path / "py"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("RAGAS_PYTHON", str(fake))
    assert ragas_service.venv_python() == fake

    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))
    assert ragas_service.venv_python() is None


def test_available_false_without_venv(monkeypatch, tmp_path):
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))
    assert ragas_service.available() is False


async def test_score_samples_degrades_when_venv_missing(monkeypatch, tmp_path):
    """隔离环境不存在 → 返回 available=False + 可行动的提示，**不抛异常**。"""
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))

    result = await ragas_service.score_samples([
        {"user_input": "问", "response": "答", "retrieved_contexts": ["c"]},
    ])

    assert result["available"] is False
    assert result["ok"] is False
    assert result["rows"] == []
    assert any("评测与ragas.md" in e for e in result["errors"]), \
        "错误提示要告诉人下一步去哪儿看（否则只会看到『指标算不出来』）"


async def test_score_samples_empty_short_circuits(monkeypatch, tmp_path):
    """空样本直接返回，不去起子进程（也不因缺环境而报 unavailable）。"""
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))

    result = await ragas_service.score_samples([])

    assert result == {"available": True, "ok": True, "rows": [], "means": {},
                      "errors": [], "ms": 0}


def test_metric_names_match_runner():
    """服务端与 runner 的指标名必须一致 —— 不一致会让整批指标静默变 None。"""
    runner_src = ragas_service.runner_script().read_text(encoding="utf-8")
    for name in ragas_service.METRIC_NAMES:
        assert f'"{name}"' in runner_src, f"{name} 在 runner 里找不到"


@pytest.mark.parametrize("name", [
    "faithfulness", "answer_relevancy", "context_precision", "context_recall",
])
def test_metric_names_are_the_ragas_four(name):
    assert name in ragas_service.METRIC_NAMES
