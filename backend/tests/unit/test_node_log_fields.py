"""图节点日志字段。"""
from __future__ import annotations

import logging

from app.graph.builder import traced
from app.graph.node_io import node_log_fields


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
