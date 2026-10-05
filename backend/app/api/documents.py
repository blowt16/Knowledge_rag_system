"""管理端文档路由（§3.7.3）。

**分层**（方案 §3.1）：本文件只做参数校验与响应封装，
上传流程与进度流在 `services/document_service.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse

from app.core.deps import require_role
from app.core.exceptions import NotFound
from app.core.telemetry import current_span_id, current_trace_id
from app.services import document_service as docs

router = APIRouter(prefix="/api/admin/documents", tags=["admin:documents"])

AdminUser = Depends(require_role("admin"))

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


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
    filename = file.filename or "unnamed"
    trace_id, span_id = current_trace_id(), current_span_id()

    task_id, batch_id = await docs.create_upload(
        filename=filename,
        content=await file.read(),
        uploader_id=user.id,
        trace_id=trace_id,
        title=title,
        visibility=visibility,
        visible_roles=visible_roles,
        effective_date=effective_date,
    )

    req = docs.build_request(
        task_id=task_id, batch_id=batch_id, filename=filename,
        uploader_id=user.id, trace_id=trace_id, title=title,
        visibility=visibility, visible_roles=visible_roles,
        effective_date=effective_date,
    )
    docs.schedule_ingest(req, trace_id=trace_id, span_id=span_id)

    return {"batch_id": batch_id,
            "tasks": [{"task_id": task_id, "file_name": filename}]}


@router.get("/upload/{task_id}/stream")
async def upload_progress(task_id: str, user=AdminUser):
    """单文件上传进度 SSE。协议细节见 `document_service.stream_progress`。"""
    if not await docs.task_exists(task_id):
        raise NotFound("任务不存在")
    return StreamingResponse(docs.stream_progress(task_id),
                             media_type="text/event-stream", headers=SSE_HEADERS)
