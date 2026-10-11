"""告警后端自算（【方案 §8.3】方案 A）。

**为什么不用 Langfuse Alerts**：实测告警**没有公开 API**（6 个候选端点全 404），
所以「后端读告警状态 → 仪表盘标红」这条路走不通，改为后端拿指标自己比阈值。
"""
from __future__ import annotations

from app.services import stats_service


def test_no_firing_when_all_normal():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.0, "node_latency": [],
    })
    assert result["firing"] == 0
    assert isinstance(result["rules"], list)


def test_node_p95_over_threshold_fires():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.0,
        "node_latency": [{"node": "rerank", "p95": 15000.0}],
    })
    assert result["firing"] >= 1
    names = [r["name"] for r in result["rules"] if r["state"] == "firing"]
    assert any("rerank" in n for n in names)


def test_error_rate_over_threshold_fires():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.9, "node_latency": [],
    })
    firing = [r["name"] for r in result["rules"] if r["state"] == "firing"]
    assert any("Http" in n or "Error" in n for n in firing)


def test_none_values_do_not_fire():
    """没有数据时不能误报 —— 刚起服务、还没有流量是正常状态。"""
    result = stats_service._evaluate_alerts({
        "latency_p95": None, "error_rate": None, "node_latency": [],
    })
    assert result["firing"] == 0
