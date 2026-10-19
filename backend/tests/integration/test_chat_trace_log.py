"""对话入口的控制台 trace_id 日志。

用户排查 Langfuse 时需要直接从后端控制台拿到本轮 trace_id；
这条日志必须和响应头 `x-trace-id` 一致，否则抄下来也搜不到。
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import observability, trace
from app.core.deps import UserContext, current_user
from app.services import chat_service
from tests.lf_capture import CapturingLangfuse


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_chat_stream_logs_the_same_trace_id_as_response_header(
        capture, monkeypatch, caplog):
    """控制台日志里的 trace_id 必须等于响应头 —— 用户拿它去平台搜。"""
    from app.api.chat import router
    from app.main import trace_middleware

    async def _fake_stream(**_kwargs):
        logging.getLogger("test.chat_generator").info(
            "inside generator",
            extra={"event": "chat.inside", "trace_id": trace.current_trace_id()},
        )
        yield 'event: done\ndata: {}\n\n'

    monkeypatch.setattr(chat_service, "stream_chat", _fake_stream)

    app = FastAPI()
    app.middleware("http")(trace_middleware)
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: UserContext(
        id="u1", username="u1", role="student", token_version=0)

    with TestClient(app) as tc:
        with caplog.at_level(logging.INFO):
            response = tc.post("/api/chat/stream",
                               json={"query": "转专业条件",
                                     "request_id": "test-req-1"})

    assert response.status_code == 200, response.text
    header_trace_id = response.headers.get("x-trace-id", "")
    assert header_trace_id and header_trace_id != "0" * 32, "响应头没带 trace_id"

    starts = [r for r in caplog.records if getattr(r, "event", "") == "chat.start"]
    assert starts, "对话开始时没有打 chat.start 日志"
    assert header_trace_id in starts[0].getMessage(), (
        f"日志里的 trace_id 与响应头不一致：{starts[0].getMessage()!r} "
        f"vs {header_trace_id}"
    )

    inside = [r for r in caplog.records if getattr(r, "event", "") == "chat.inside"]
    assert inside, "SSE 生成器里没有日志"
    assert inside[0].trace_id == header_trace_id, (
        f"生成器里的 trace_id 与响应头不一致：{inside[0].trace_id!r} "
        f"vs {header_trace_id!r}"
    )
