"""后台摄入调度日志。"""
from __future__ import annotations

import asyncio
import logging

from app.ingestion.pipeline import IngestRequest
from app.services import document_service


async def test_schedule_ingest_logs_and_runs(tmp_path, monkeypatch, caplog):
    started = asyncio.Event()

    async def fake_ingest(req):
        started.set()

    monkeypatch.setattr(document_service, "ingest", fake_ingest)

    source = tmp_path / "x.txt"
    source.write_text("x", encoding="utf-8")
    req = IngestRequest(
        source_path=source,
        filename="x.txt",
        uploader_id="u",
        task_id="t",
        batch_id="b",
        trace_id="a" * 32,
    )

    with caplog.at_level(logging.INFO, logger="app.services.document_service"):
        document_service.schedule_ingest(req, trace_id="a" * 32, span_id="b" * 16)
        await asyncio.wait_for(started.wait(), timeout=2)

    recs = [r for r in caplog.records
            if getattr(r, "event", "") == "ingest.schedule"]
    assert recs, "没有 ingest.schedule 日志"
    assert recs[-1].doc_filename == "x.txt"
