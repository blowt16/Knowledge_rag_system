"""Langfuse 客户端底座（【方案 §12.1 修正 2/3】）。

两条都是实测踩出来的：
  1. 未配置密钥时 `get_current_trace_id()` 返回 None 而不是全 0 串
     —— 不兜底会让日志字段变成 `"trace_id": null`，破坏既有契约。
  2. SDK 的 host 缺省值是 https://cloud.langfuse.com
     —— 配了 key 漏配地址，数据会静默发去公有云。
"""
from __future__ import annotations

import logging
import os

import pytest

from app.core import observability
from app.core.logging import JsonFormatter


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


def test_enabling_client_does_not_recurse_through_logging(monkeypatch):
    """★ 回归：启用客户端后打一行日志，formatter 只能被调用有限次。

    实测过的失败形态：SDK 在「无 active span」时 warning，
    而 formatter 会在 format() 里调 current_trace_id() → warning 又走回
    root handler → format() → 递归。护栏没生效时这里会看到几十次调用
    （实测峰值 51，不含护栏则直到 RecursionError）。

    ⚠️ handler 必须**真的走 format()**。覆写 `emit` 成 `seen.append(record)`
       会让 formatter 一次都不被调用，用例永远绿 —— 那是假凭据。
       （Task 4 换源前 `JsonFormatter` 直读 OTel，递归支路不在它身上，
       这里挂的是等价模拟件；换源后挂的就是真的 `JsonFormatter`。）
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
    # 指向一个必然连不上的地址，逼出 SDK 的失败路径（正是递归的触发源）
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    observability._reset_for_test()
    observability.setup("campus-rag")

    seen: list[logging.LogRecord] = []

    handler = logging.Handler()

    def _emit(record: logging.LogRecord) -> None:
        seen.append(record)
        # 给测量封顶：护栏失效时递归会一路跑到 RecursionError（几万行 traceback），
        # 封顶后停在 20 次，断言照样红，但输出可读。
        if len(seen) < 20:
            handler.format(record)

    handler.emit = _emit  # type: ignore[method-assign]
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        root.warning("一行应用日志")
    finally:
        root.removeHandler(handler)
        observability.shutdown()

    assert len(seen) < 5, (
        f"日志 formatter 被调用 {len(seen)} 次 —— 递归护栏没生效"
    )
