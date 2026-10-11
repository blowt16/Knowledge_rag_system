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
# 修复轮：拆 token **不能靠 `usageType` 维度**（实测 2026-10-11，真项目）
# —— `usageType` 分组回的三行只是**同一个 `totalTokens` 的副本**；
#   真正的拆分是**三个 measure 一行**：inputTokens / outputTokens / totalTokens。
#   以上是简报原文，以下是实测后追加（【方案 §8.2】的 token 拆输入/输出同批补上）。
# ============================================================

#: 实测原文（真项目查询返回的形状）：**一行**，三个 measure 分列 —— 7 / 3 / 10。
_USAGE_ONE_ROW = {"data": [{"sum_inputTokens": 7, "sum_outputTokens": 3,
                            "sum_totalTokens": 10, "sum_totalCost": 0}]}


def test_usage_tokens_reads_the_three_measures():
    """★ 拆分是**真拆**：input/output 各有自己的字段，不是总量的副本。

    ⚠️ `input != total` / `output != total` 两条断言必须有：历史坑是
       `dimensions=[usageType]` 把同一个总量回成三行（10/10/10）——
       实现若退化成「三个字段读同一个数」，只断言 total 的用例不会红。
    """
    total, inp, out = stats_service._usage_tokens(_USAGE_ONE_ROW)
    assert total == 10, f"总量取 sum_totalTokens（实得 {total}）"
    assert inp == 7
    assert out == 3
    assert inp != total and out != total, "拆分退化成副本（==总量）必须红"


def test_usage_tokens_handles_empty_and_null():
    """与 `_extract_scalar` 同口径：两种空结果形态、以及字段为 null，都不许抛异常。"""
    assert stats_service._usage_tokens(None) == (None, None, None)
    assert stats_service._usage_tokens({"data": []}) == (None, None, None)
    assert stats_service._usage_tokens(
        {"data": [{"sum_inputTokens": None, "sum_outputTokens": None,
                   "sum_totalTokens": None}]}
    ) == (None, None, None)
