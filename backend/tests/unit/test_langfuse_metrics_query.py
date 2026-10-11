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


# ============================================================
# 修复轮：`usageType` 分组会回**三行**（评审 2026-10-11 对真项目实测所得）
# —— 以上是简报原文，以下是实测后追加（【方案 §8.2】的 token 拆输入/输出同批补上）
# ============================================================

#: 实测原文（真项目查询返回的形状）：三行，带的是**同一个总量**。
_USAGE_THREE_ROWS = {"data": [
    {"usageType": "total", "sum_totalTokens": 10},
    {"usageType": "input", "sum_totalTokens": 10},
    {"usageType": "output", "sum_totalTokens": 10},
]}


def test_usage_tokens_does_not_sum_the_three_rows():
    """★ 三行不能全加 —— 全加会把 token 用量报成 **3 倍**，而且看不出哪里不对。"""
    total, inp, out = stats_service._usage_tokens(_USAGE_THREE_ROWS)
    assert total == 10, f"有 total 行就用它，全加会变成 30（实得 {total}）"
    assert inp == 10
    assert out == 10


def test_usage_tokens_falls_back_to_input_plus_output():
    """没有 `total` 行时才用 input+output 兜底（这里两行值不同，顺带锁住映射关系）。"""
    payload = {"data": [{"usageType": "input", "sum_totalTokens": 4},
                        {"usageType": "output", "sum_totalTokens": 6}]}
    assert stats_service._usage_tokens(payload) == (10.0, 4.0, 6.0)


def test_usage_tokens_handles_empty_and_null():
    """与 `_extract_scalar` 同口径：两种空结果形态、以及字段为 null，都不许抛异常。"""
    assert stats_service._usage_tokens(None) == (None, None, None)
    assert stats_service._usage_tokens({"data": []}) == (None, None, None)
    assert stats_service._usage_tokens(
        {"data": [{"usageType": "total", "sum_totalTokens": None}]}
    ) == (None, None, None)


def test_usage_total_takes_the_total_row_not_the_sum():
    """成本是**同一个陷阱**（实测三行都带同一个 `sum_totalCost`）：
    取 `total` 行；只有 input/output 行时返回 None —— 宁可显示「—」，也不报 3 倍。"""
    payload = {"data": [{"usageType": t, "sum_totalCost": 0.25}
                        for t in ("total", "input", "output")]}
    assert stats_service._usage_total(payload, "sum_totalCost") == 0.25
    assert stats_service._usage_total(
        {"data": [{"usageType": "input", "sum_totalCost": 0.25}]},
        "sum_totalCost") is None
