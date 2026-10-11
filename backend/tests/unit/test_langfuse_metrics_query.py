"""Metrics API v2 查询构造（【方案 §8.1】）。

**形状全部是实测来的**（【方案 §12.4】验收 D）：
  - `query` 是**一个 JSON 字符串参数**，不是散装参数
  - `fromTimestamp` / `toTimestamp` **必填**
  - 返回字段是 `<聚合>_<指标>` 拼接
  - 空结果有**两种形态**：`[{"count_count":0,"p95_latency":null}]` 与 `[]`
    —— 前者 latency 是 `null` 不是 0，直接 float() 会抛异常
"""
from __future__ import annotations

import json

import pytest

from app.services import stats_service


def test_query_is_a_single_json_param():
    q = stats_service._build_metrics_query(
        measures=[("latency", "p95")],
        dimensions=None, hours=24,
    )
    assert isinstance(q, str)
    parsed = json.loads(q)
    assert parsed["view"] == "observations"
    assert parsed["metrics"] == [{"measure": "latency", "aggregation": "p95"}]
    assert "fromTimestamp" in parsed and "toTimestamp" in parsed, "时间戳必填"


def test_query_with_dimension():
    q = stats_service._build_metrics_query(
        measures=[("latency", "p95")],
        dimensions=["name"], hours=24,
    )
    assert json.loads(q)["dimensions"] == [{"field": "name"}]


@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"p95_latency": 1234.5}]}, 1234.5),
    ({"data": [{"p95_latency": None}]}, None),        # ← 空结果的形态一
    ({"data": []}, None),                              # ← 空结果的形态二
    ({"data": [{}]}, None),
])
def test_extract_scalar_handles_both_empty_shapes(payload, expected):
    """★ 两种空结果都不能抛异常（【方案 §8.1】记的那个坑的放大版）。"""
    assert stats_service._extract_scalar(payload, "p95_latency") == expected


def test_extract_scalar_keeps_real_zero():
    """正控：真的是 0 时要返回 0，不能跟「没数据」混为一谈。"""
    assert stats_service._extract_scalar({"data": [{"count_count": 0}]},
                                         "count_count") == 0
