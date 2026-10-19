"""图节点日志字段。"""
from __future__ import annotations

import logging

from app.core import observability, trace
from app.graph import node_io
from app.graph.builder import traced
from app.graph.node_io import node_log_fields
from app.graph.state import Chunk


def test_node_log_fields_have_no_body():
    state = {"query": "转专业条件是什么"}
    result = {
        "resolved_query": "转专业的条件是什么",
        "candidates": [object(), object()],
        "trace": [{"node": "resolve", "ms": 12, "recalled": 0, "degraded": None}],
    }
    fields = node_log_fields("resolve", result, state)
    assert fields["query_len"] == len(state["query"])
    assert fields["changed"] is True
    assert "转专业" not in repr(fields)


async def test_traced_logs_even_without_langfuse(caplog):
    async def fake(state):
        return {
            "route": "knowledge",
            "route_source": "rules",
            "trace": [{"node": "route", "ms": 3, "recalled": 0, "degraded": None}],
        }

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        result = await traced("route", fake)({"query": "q"})

    assert result["route"] == "knowledge"
    recs = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"]
    assert recs, "没有节点完成日志"
    assert recs[-1].stage == "node.route"
    assert recs[-1].route == "knowledge"


async def test_debug_details_for_resolve_rewrite_rerank(caplog):
    observability._reset_for_test()

    async def resolve_node(state):
        return {
            "resolved_query": "转专业的申请条件",
            "resolve_skipped_reason": "",
            "trace": [{"node": "resolve", "ms": 1, "recalled": 0,
                       "degraded": None}],
        }

    async def rewrite_node(state):
        return {
            "retrieval_queries": [
                {"text": "转专业申请条件", "source": "verbatim",
                 "target": "both", "weight": 1.0},
                {"text": "转专业 条件", "source": "keywords",
                 "target": "bm25", "weight": 1.0},
            ],
            "trace": [{"node": "rewrite", "ms": 1, "recalled": 2,
                       "degraded": None}],
        }

    chunks = [
        Chunk(chunk_id="c1", text="第一段正文：申请条件", score=0.91,
              document_name="转专业办法", page=3),
        Chunk(chunk_id="c2", text="第二段正文：办理流程", score=0.72,
              document_name="转专业办法", page=4),
    ]

    async def rerank_node(state):
        return {
            "reranked": chunks,
            "retrieval_confidence": 0.91,
            "trace": [{"node": "rerank", "ms": 1, "recalled": 2,
                       "degraded": None}],
        }

    with caplog.at_level(logging.DEBUG):
        with trace.begin_trace():
            await traced("resolve", resolve_node)({"query": "它需要什么条件"})
            await traced("rewrite", rewrite_node)({"query": "转专业的申请条件"})
            await traced("rerank", rerank_node)({"query": "转专业的申请条件"})

    details = [r for r in caplog.records
               if getattr(r, "event", "") == "node.detail"]
    resolve = [r for r in details if getattr(r, "stage", "") == "resolve.detail"]
    assert resolve and resolve[-1].resolved_query == "转专业的申请条件"

    rewrite = [r for r in details if getattr(r, "stage", "") == "rewrite.detail"]
    assert {r.query for r in rewrite} == {"转专业申请条件", "转专业 条件"}
    assert {(r.source, r.target) for r in rewrite} == {
        ("verbatim", "both"), ("keywords", "bm25")}

    topk = [r for r in details if getattr(r, "stage", "") == "rerank.topk"]
    assert [(r.rank, r.chunk_id) for r in topk] == [(1, "c1"), (2, "c2")]
    assert topk[0].score == 0.91
    assert "第一段正文" in topk[0].text


async def test_content_details_are_debug_only(caplog):
    observability._reset_for_test()

    async def resolve_node(state):
        return {"resolved_query": "真实问题", "resolve_skipped_reason": ""}

    with caplog.at_level(logging.INFO):
        with trace.begin_trace():
            await traced("resolve", resolve_node)({"query": "原问题"})

    assert not [r for r in caplog.records
                if getattr(r, "event", "") == "node.detail"]


async def test_content_details_can_be_disabled(monkeypatch, caplog):
    observability._reset_for_test()
    monkeypatch.setattr(node_io, "_trace_content_enabled", lambda: False)

    async def resolve_node(state):
        return {"resolved_query": "真实问题", "resolve_skipped_reason": ""}

    with caplog.at_level(logging.DEBUG):
        with trace.begin_trace():
            await traced("resolve", resolve_node)({"query": "原问题"})

    assert not [r for r in caplog.records
                if getattr(r, "event", "") == "node.detail"]


async def test_traced_chain_emits_stage_done_for_each_node(caplog):
    async def fake(state):
        return {
            "trace": [{"node": "x", "ms": 1, "recalled": 0, "degraded": None}],
        }

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        with trace.begin_trace():
            for name in ("resolve", "retrieve", "generate"):
                await traced(name, fake)({"query": "q"})

    done = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"]
    stages = {r.stage for r in done}
    assert {"node.resolve", "node.retrieve", "node.generate"} <= stages
    assert all(r.trace_id for r in done)
