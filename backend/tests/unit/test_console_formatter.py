"""控制台文本格式器与 JSON 新增字段。"""
from __future__ import annotations

import io
import json
import logging

from app.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    _LANGFUSE_CONTEXT_NOISE,
    _force_utf8,
)


def _record(msg: str = "完成", **extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_console_line_contains_trace_stage_duration_and_fields():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(
        trace_id="a1b2c3d4" + "0" * 24, stage="retrieve", depth=1,
        event="stage.done", ms=2717, ok=True, candidates=17,
    ))
    assert "[a1b2c3d4]" in out
    assert "retrieve" in out
    assert "2.72s" in out
    assert "candidates=17" in out
    assert "{" not in out


def test_console_truncates_and_strips_newlines():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(
        trace_id="a" * 32, stage="parse", depth=0,
        event="stage.done", ms=5, ok=True,
        path="x" * 300 + "\nsecond",
    ))
    assert "\n" not in out
    assert "x" * 121 not in out
    assert "path=" in out


def test_force_utf8_reconfigures_gbk_stream():
    """Windows 控制台默认 GBK 时，✔/✖ 会触发 UnicodeEncodeError。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    _force_utf8(stream)
    assert stream.encoding.lower() == "utf-8"


def test_console_uses_latency_ms_as_duration():
    """chat.done 带的是 latency_ms，不是 ms —— 汇总行必须显示总耗时。"""
    out = ConsoleFormatter(use_color=False).format(_record(
        trace_id="a" * 32, stage="chat", event="chat.done", ok=True,
        latency_ms=2717, route="knowledge",
    ))
    assert "2.72s" in out
    assert "latency_ms=" not in out


def test_console_no_color_when_disabled():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(trace_id="a" * 32, stage="route",
                             event="stage.done", ms=8, ok=True))
    assert "\033[" not in out


def test_json_formatter_adds_stage_fields(monkeypatch):
    from app.core import observability
    monkeypatch.setattr(observability, "current_trace_id", lambda: "a" * 32)
    monkeypatch.setattr(observability, "current_span_id", lambda: "b" * 16)

    payload = json.loads(JsonFormatter().format(_record(
        stage="retrieve", depth=1, stage_id="c" * 16,
        parent_stage_id="d" * 16, event="stage.done", ms=12,
    )))
    assert payload["stage"] == "retrieve"
    assert payload["depth"] == 1
    assert payload["stage_id"] == "c" * 16
    assert payload["parent_stage_id"] == "d" * 16
    assert payload["extra"]["ms"] == 12


def test_console_shows_content_fields_with_longer_limit():
    """DEBUG 内容日志的 query/text 允许比普通字段更长。"""
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(
        trace_id="a" * 32, stage="rerank.topk", depth=1, event="node.detail",
        text="x" * 400, score=0.9, chunk_id="c1",
    ))
    assert "text=" in out
    assert "x" * 299 in out
    assert "x" * 300 not in out
    assert "chunk_id=c1" in out
    assert "score=0.9" in out


def test_content_fields_are_redacted_in_json():
    """控制台可以看正文，但 JSON 文件仍按敏感键打码。"""
    payload = json.loads(JsonFormatter().format(_record(
        event="node.detail", resolved_query="真实问题", text="片段",
        answer_preview="答案片段",
    )))
    assert payload["extra"]["resolved_query"] == "[REDACTED]"
    assert payload["extra"]["text"] == "[REDACTED]"
    assert payload["extra"]["answer_preview"] == "[REDACTED]"


def test_langfuse_context_noise_is_filtered_but_real_warnings_remain():
    noise = logging.LogRecord(
        "langfuse", logging.WARNING, __file__, 1,
        "Context error: No active span in current context.", (), None)
    real = logging.LogRecord(
        "langfuse", logging.WARNING, __file__, 1,
        "Langfuse export failed", (), None)
    assert _LANGFUSE_CONTEXT_NOISE.filter(noise) is False
    assert _LANGFUSE_CONTEXT_NOISE.filter(real) is True


def test_integer_sensitive_count_is_not_redacted():
    """messages/history 的**数量**不是正文，不能被按关键字整值打码。"""
    payload = json.loads(JsonFormatter().format(_record(
        messages=3, history=5, query="学号2023123456", event="test",
    )))
    assert payload["extra"]["messages"] == 3
    assert payload["extra"]["history"] == 5
    assert payload["extra"]["query"] == "[REDACTED]"


def test_json_formatter_still_scrubs_extra():
    payload = json.loads(JsonFormatter().format(_record(
        query="学号2023123456", event="test",
    )))
    assert "2023123456" not in json.dumps(payload, ensure_ascii=False)
