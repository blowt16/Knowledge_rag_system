"""本地 trace/span 上下文。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.core import trace


def test_new_ids_are_hex():
    tid = trace.new_trace_id()
    sid = trace.new_span_id()
    assert len(tid) == 32 and int(tid, 16) >= 0
    assert len(sid) == 16 and int(sid, 16) >= 0


def test_begin_trace_sets_and_resets():
    assert trace.current_trace_id() == ""
    with trace.begin_trace("a" * 32) as tid:
        assert tid == "a" * 32
        assert trace.current_trace_id() == "a" * 32
    assert trace.current_trace_id() == ""


def test_nested_spans_parent_and_depth():
    with trace.begin_trace():
        assert trace.current_depth() == 0
        with trace.trace_span("outer") as outer:
            assert outer.depth == 0
            assert trace.current_stage() == "outer"
            with trace.trace_span("inner") as inner:
                assert inner.depth == 1
                assert inner.parent_span_id == outer.span_id
                assert trace.current_stage() == "inner"
            assert trace.current_stage() == "outer"
        assert trace.current_stage() == ""


async def test_new_trace_does_not_inherit_parent_span():
    """后台/SSE 里开新 trace 时，不能继承外层已关闭的 span 栈。"""
    with trace.begin_trace():
        with trace.trace_span("http"):
            async def child() -> tuple[int, str, str]:
                with trace.begin_trace():
                    with trace.trace_span("inner") as inner:
                        return inner.depth, inner.parent_span_id, trace.current_stage()

            return_value = await asyncio.create_task(child())
            assert return_value == (0, "", "inner")


async def test_concurrent_traces_do_not_cross():
    async def worker(tid: str, stage: str) -> tuple[str, str]:
        with trace.begin_trace(tid):
            with trace.trace_span(stage):
                await asyncio.sleep(0)
                return trace.current_trace_id(), trace.current_stage()

    first, second = await asyncio.gather(
        worker("a" * 32, "one"),
        worker("b" * 32, "two"),
    )
    assert first == ("a" * 32, "one")
    assert second == ("b" * 32, "two")


async def test_trace_propagates_to_created_task():
    with trace.begin_trace("b" * 32):
        async def child() -> str:
            return trace.current_trace_id()

        assert await asyncio.create_task(child()) == "b" * 32


def test_span_logs_done_with_fields(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        with trace.begin_trace():
            with trace.trace_span("retrieve", layer="stage", candidates=17):
                pass

    done = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"]
    assert done, "没有 stage.done 日志"
    rec = done[-1]
    assert rec.stage == "retrieve"
    assert rec.layer == "stage"
    assert rec.ok is True
    assert rec.ms >= 0
    assert rec.candidates == 17
    assert len(rec.trace_id) == 32


def test_span_logs_failure_and_reraises(caplog):
    with caplog.at_level(logging.ERROR, logger="app.core.trace"):
        with trace.begin_trace():
            with pytest.raises(RuntimeError):
                with trace.trace_span("bad"):
                    raise RuntimeError("boom")

    failed = [r for r in caplog.records if getattr(r, "event", "") == "stage.failed"]
    assert failed, "没有 stage.failed 日志"
    assert failed[-1].stage == "bad"
    assert failed[-1].error == "RuntimeError"
    assert failed[-1].ok is False


def test_span_set_updates_final_fields(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        with trace.begin_trace():
            with trace.trace_span("retrieve") as span:
                span.set(candidates=5, degraded=None)

    rec = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"][-1]
    assert rec.candidates == 5
    assert rec.degraded is None
