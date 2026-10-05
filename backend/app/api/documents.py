"""管理端文档接口（§3.7.3 / §3.7.2 的上传规格）。

**上传粒度**：一次 zip = 一个 `batch_id` + N 条 `ingestion_tasks`（每条对应包内一个文件）。
单文件上传时 `batch_id = task_id`、`tasks` 只有一项 —— **响应结构统一**，
前端不必写两套。

**为什么需要 batch 级进度流**：一个 zip 有 N 个文件，**开 N 条 SSE 流是不现实的**。
`upload/batch/{batch_id}/stream` 聚合成一条流。

⚠️ M0 先实现单文件上传 + 单文件进度流；zip 批量与批次进度流在 M4 补
   （它们只影响管理端体验，不阻塞 M0 的端到端验收）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app import db
from app.core.config import cfg
from app.core.deps import require_role
from app.core.exceptions import NotFound
from app.core.telemetry import current_span_id, current_trace_id, reattach
from app.ingestion.pipeline import IngestRequest, ingest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/documents", tags=["admin:documents"])

AdminUser = Depends(require_role("admin"))


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    visibility: str = Form("public"),
    visible_roles: str = Form(""),          # 逗号分隔，如 "admin,staff"
    effective_date: str | None = Form(None),
    user=AdminUser,
):
    """单文件上传。返回 `{batch_id, tasks:[{task_id, file_name}]}`。"""
    task_id = uuid.uuid4().hex
    batch_id = task_id           # 单文件时 batch_id = task_id，口径统一

    tmp_dir = Path(cfg("logging.dir", "logs")).parent / "data" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{task_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    roles = [r.strip() for r in (visible_roles or "").split(",") if r.strip()]
    if visibility == "restricted" and not roles:
        # 「受限」至少要勾一个角色，否则谁也看不到（且不会报错）
        roles = ["admin"]

    effective = None
    if effective_date:
        try:
            effective = date.fromisoformat(effective_date)
        except ValueError:
            effective = None

    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (id, batch_id, file_name, status, progress, trace_id, uploader_id)
               VALUES ($1,$2,$3,'pending',0,$4,$5)""",
            task_id, batch_id, file.filename or "unnamed",
            current_trace_id(), user.id,
        )

    req = IngestRequest(
        source_path=tmp_path,
        filename=file.filename or "unnamed",
        uploader_id=user.id,
        task_id=task_id,
        batch_id=batch_id,
        trace_id=current_trace_id(),
        title=title,
        visibility=visibility if visibility in ("public", "restricted") else "public",
        visible_roles=roles,
        effective_date=effective,
    )

    # ⚠️ 后台任务不能靠 contextvars 隐式继承 —— trace_id 已落库，
    #    这里显式挂回来（§3.2.3.1）
    trace_id, span_id = current_trace_id(), current_span_id()

    async def _background() -> None:
        with reattach(trace_id, span_id):
            try:
                await ingest(req)
            finally:
                tmp_path.unlink(missing_ok=True)

    # M0 用进程内任务；M4 换成任务表驱动的 worker（进度流已按表实现，接口不变）
    asyncio.create_task(_background())

    return {"batch_id": batch_id,
            "tasks": [{"task_id": task_id, "file_name": file.filename}]}


@router.get("/upload/{task_id}/stream")
async def upload_progress(task_id: str, user=AdminUser):
    """单文件上传进度 SSE。

    ⚠️ 这是与 chat 流**互相独立的一套协议**，不要复用 chat 的事件语义：
       chat 的 `error` 是**终止事件**，而这里单个文件失败**不影响整包继续**。

    ⚠️ `counts` 的键**恰好是 `ingestion_tasks.status` 的 7 个取值**，一个不多一个不少。
    """
    import json

    from fastapi.responses import StreamingResponse

    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM ingestion_tasks WHERE id = $1", task_id)
    if row is None:
        raise NotFound("任务不存在")

    async def generator():
        last_status = None
        while True:
            async with db.tx() as conn:
                task = await conn.fetchrow(
                    """SELECT status, progress, message, error, document_id
                         FROM ingestion_tasks WHERE id = $1""",
                    task_id,
                )
            if task is None:
                break
            if task["status"] != last_status:
                last_status = task["status"]
                payload = {
                    "done": 1 if task["status"] == "done" else 0,
                    "total": 1,          # 单文件时 total = 1
                    "current_file": task["message"] or "",
                    "counts": _counts(task["status"]),
                }
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

            if task["status"] in ("done", "duplicate", "failed"):
                yield ("event: done\ndata: "
                       + json.dumps({"batch_id": task_id,
                                     "counts": _counts(task["status"])},
                                    ensure_ascii=False) + "\n\n")
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _counts(status: str) -> dict[str, int]:
    """键恰好是 ingestion_tasks.status 的 7 个取值。"""
    keys = ["pending", "parsing", "chunking", "embedding", "done", "duplicate", "failed"]
    return {k: (1 if k == status else 0) for k in keys}
