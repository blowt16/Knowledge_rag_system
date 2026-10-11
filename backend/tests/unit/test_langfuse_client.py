"""Langfuse 客户端底座（【方案 §12.1 修正 2/3】）。

两条都是实测踩出来的：
  1. 未配置密钥时 `get_current_trace_id()` 返回 None 而不是全 0 串
     —— 不兜底会让日志字段变成 `"trace_id": null`，破坏既有契约。
  2. SDK 的 host 缺省值是 https://cloud.langfuse.com
     —— 配了 key 漏配地址，数据会静默发去公有云。
"""
from __future__ import annotations

import os

import pytest

from app.core import observability


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「没有 Langfuse 配置」的干净状态开始。"""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    yield
    observability._reset_for_test()


def test_current_trace_id_is_all_zero_without_credentials():
    """没配密钥 → 全 0 串，**不是 None**（这是日志契约的底线）。"""
    observability.setup("campus-rag")
    tid = observability.current_trace_id()
    assert isinstance(tid, str)
    assert tid == "0" * 32


def test_setup_without_credentials_does_not_raise():
    """跑测试/CLI 时没密钥是正常状态，不许抛异常。"""
    observability.setup("campus-rag")      # 不抛即通过


def test_missing_base_url_disables_instead_of_falling_back(monkeypatch):
    """★ 关键：配了 key 但**没配地址**时，宁可不启用，也**不得**落到公有云默认值。

    SDK 源码的解析顺序是
        base_url or LANGFUSE_BASE_URL or host or LANGFUSE_HOST or "https://cloud.langfuse.com"
    —— 落在最后一档就是把数据发去公有云。「不启用」才是安全的那条路。
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
    # 故意不设 LANGFUSE_BASE_URL

    observability.setup("campus-rag")

    assert observability.resolved_base_url() is None
    assert observability.get_client() is None, (
        "没配地址时必须走「不启用」分支 —— 否则 SDK 会用缺省的公有云地址"
    )


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.edu")
    observability._reset_for_test()
    assert observability.resolved_base_url() == "https://lf.example.edu"


def test_span_id_is_all_zero_without_credentials():
    observability.setup("campus-rag")
    assert observability.current_span_id() == "0" * 16
