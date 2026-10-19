"""LLM 调用的阶段日志。"""
from __future__ import annotations

import logging

from app.core import llm


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"content": "你好"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _Resp()


class _StreamResponse:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"你"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"好"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}'
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _StreamCtx:
    async def __aenter__(self):
        return _StreamResponse()

    async def __aexit__(self, *args):
        return False


class _StreamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return _StreamCtx()


async def test_complete_logs_tokens_without_prompt(monkeypatch, caplog):
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm, "_base", lambda: "http://example.test")
    monkeypatch.setattr(llm, "_headers", lambda: {})

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        text = await llm.complete(
            [{"role": "user", "content": "SECRET_PROMPT_9f3a"}],
            timeout=1,
        )

    assert text == "你好"
    recs = [r for r in caplog.records if getattr(r, "stage", "") == "llm"
            and getattr(r, "event", "") == "stage.done"]
    assert recs, "没有 llm 完成日志"
    rec = recs[-1]
    assert rec.tokens_in == 10
    assert rec.tokens_out == 2
    assert rec.chars == 2
    assert "SECRET_PROMPT_9f3a" not in caplog.text


async def test_stream_logs_ttft_and_tokens(monkeypatch, caplog):
    monkeypatch.setattr(llm.httpx, "AsyncClient", _StreamClient)
    monkeypatch.setattr(llm, "_base", lambda: "http://example.test")
    monkeypatch.setattr(llm, "_headers", lambda: {})

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        pieces = [p async for p in llm.stream_raw(
            [{"role": "user", "content": "SECRET_PROMPT_9f3a"}], timeout=1)]

    assert "".join(pieces) == "你好"
    recs = [r for r in caplog.records if getattr(r, "stage", "") == "llm"
            and getattr(r, "event", "") == "stage.done"]
    assert recs, "没有 llm 流式完成日志"
    rec = recs[-1]
    assert rec.ttft_ms is not None and rec.ttft_ms >= 0
    assert rec.tokens_in == 10
    assert rec.tokens_out == 2
    assert rec.chars == 2
    assert "SECRET_PROMPT_9f3a" not in caplog.text
