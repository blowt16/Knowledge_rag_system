"""日志的 trace_id 契约（【方案 §6】）。

⚠️ 改动前后都必须成立：**无有效 trace 时是全 0 串，不是 null**。
前端错误提示、按 trace_id 检索日志都依赖它是合法字符串。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core import observability
from app.core.logging import JsonFormatter


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    yield
    observability._reset_for_test()


def _format(msg: str, **extra) -> dict:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return json.loads(JsonFormatter().format(record))


def test_trace_id_is_legal_string_without_langfuse():
    """★ 未配置密钥时也要是合法的 32 位十六进制串，**不能是 null**。"""
    observability.setup("campus-rag")
    payload = _format("hello")

    assert payload["trace_id"] is not None
    assert isinstance(payload["trace_id"], str)
    assert payload["trace_id"] == "0" * 32
    assert payload["span_id"] == "0" * 16


def test_trace_id_is_32_hex_when_enabled(monkeypatch):
    """启用后必须是 32 位十六进制（不是 UUID、不带横线）。"""
    from tests.lf_capture import CapturingLangfuse
    import re

    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")

        client = observability.get_client()
        with client.start_as_current_observation(as_type="span", name="x"):
            payload = _format("hello")
        observability.shutdown()

    assert re.fullmatch(r"[0-9a-f]{32}", payload["trace_id"]), payload["trace_id"]
    assert payload["trace_id"] != "0" * 32


def test_local_trace_id_is_used_inside_trace():
    """请求/任务内使用本地 trace_id，不再退化成全 0。"""
    from app.core import trace

    with trace.begin_trace():
        payload = _format("hello")

    assert len(payload["trace_id"]) == 32
    assert payload["trace_id"] != "0" * 32


def test_extra_fields_still_scrubbed():
    """回归：改造不能碰坏既有的 extra 脱敏。"""
    payload = _format("q", query="学号2023123456", event="test")
    assert payload["event"] == "test"
    assert "2023123456" not in json.dumps(payload, ensure_ascii=False)


def test_log_formatter_reads_from_observability_not_otel(monkeypatch):
    """★ 锁住「换源」这件事本身。

    ⚠️ 为什么需要这条：Langfuse SDK **本身建在 OTel 之上**，所以
       `current_trace_id()` 与直接读 OTel 当前 span 返回的 id **逐字节相同** ——
       上面那条 `test_trace_id_is_32_hex_when_enabled` 因此**区分不了数据来源**，
       把 `logging.py` 改回直读 OTel 它照样绿。

       这里用一个**不可能从 OTel 里读到的哨兵值**把来源钉死：
       只要格式化器还走 observability，日志里就必须出现这个哨兵。
    """
    sentinel = "sentinel" + "0" * 24          # 32 位，形状合法但绝不可能是真 id
    monkeypatch.setattr(observability, "current_trace_id", lambda: sentinel)
    monkeypatch.setattr(observability, "current_span_id", lambda: "s" * 16)

    payload = _format("hello")

    assert payload["trace_id"] == sentinel, (
        "日志没走 observability.current_trace_id() —— "
        "多半是 logging.py 又回去直读 OTel 了"
    )
