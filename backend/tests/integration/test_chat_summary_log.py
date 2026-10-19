"""chat 首字与完成汇总日志。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.core.deps import UserContext
from app.services import chat_service
from tests.integration.test_stage_events import (
    ScriptedGraph,
    _frames,
    _never_disconnected,
    script_for,
    session,  # noqa: F401 —— pytest fixture
    task_start,
)


@pytest.fixture(autouse=True)
def _install(monkeypatch):
    def install(script):
        monkeypatch.setattr(chat_service, "get_graph", lambda: ScriptedGraph(script))

    yield install
    monkeypatch.undo()


def _final() -> dict:
    return {
        "route": "knowledge", "refused": False, "decision": "ANSWERED",
        "answer": "答案", "citations": [],
        "reranked": [SimpleNamespace(chunk_id="c1")],
    }


async def test_chat_done_logs_summary(session, _install, caplog):
    final = _final()
    _install(script_for(["resolve", "route", "generate", "cite"], final))

    with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
        await _frames(session)

    done = [r for r in caplog.records if getattr(r, "event", "") == "chat.done"]
    assert done, "没有 chat.done 日志"
    rec = done[-1]
    assert rec.stage == "chat"
    assert rec.route == "knowledge"
    assert rec.decision == "ANSWERED"
    assert rec.latency_ms >= 0


async def test_chat_logs_do_not_leak_query(session, _install, caplog):
    final = _final()
    _install(script_for(["resolve", "route", "generate"], final))

    with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
        frames = [
            frame async for frame in chat_service.stream_chat(
                query="SECRET_QUERY_9f3a",
                session_id=session,
                is_new_session=False,
                include_restricted=False,
                user=UserContext(id="u", username="u", role="student",
                                 token_version=0),
                is_disconnected=_never_disconnected,
            )
        ]

    assert frames
    assert "SECRET_QUERY_9f3a" not in caplog.text


async def test_chat_first_token_logs_ttft(session, _install, caplog):
    final = _final()
    script = [
        task_start("resolve"),
        ("values", final),
        task_start("route"),
        ("values", final),
        task_start("generate"),
        ("custom", {"type": "token", "text": "你"}),
        ("values", final),
    ]
    _install(script)

    with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
        await _frames(session)

    first = [r for r in caplog.records
             if getattr(r, "event", "") == "chat.first_token"]
    assert first, "没有 chat.first_token 日志"
    assert first[-1].ttft_ms is not None
    assert first[-1].ttft_ms >= 0
