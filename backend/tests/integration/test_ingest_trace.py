"""文档摄入的阶段日志。"""
from __future__ import annotations

import logging
import uuid

from tests.support import delete_admin, ingest_text_doc, insert_admin, purge_documents_by_id


async def test_ingest_emits_stage_logs(caplog):
    admin_id = await insert_admin()
    title = f"trace_test_{uuid.uuid4().hex[:8]}"
    text = ("校园网缴费流程：登录统一身份认证，进入缴费页面，选择项目并完成支付。\n" * 30)
    doc_id = None
    try:
        with caplog.at_level(logging.INFO, logger="app.core.trace"):
            doc_id = await ingest_text_doc(
                admin_id=admin_id, title=title, text=text)

        stages = {getattr(r, "stage", "") for r in caplog.records
                  if getattr(r, "event", "") == "stage.done"}
        assert {
            "ingest.validate", "ingest.dedupe", "ingest.store", "ingest.parse",
            "ingest.clean", "ingest.chunk", "ingest.enrich", "ingest.embed",
            "ingest.index_write", "ingest.activate",
        } <= stages, f"缺少摄入阶段日志：{sorted(stages)}"
    finally:
        if doc_id:
            await purge_documents_by_id([doc_id])
        await delete_admin(admin_id)
