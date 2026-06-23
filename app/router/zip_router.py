"""压缩包上传与任务查询路由。"""
import os
import json
import uuid
import asyncio
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from app.config.loader import get_config
from app.rag.zip_handler.zip_handler import ZipTaskManager
from app.core.success_response import success_response
from app.core.failed_response import AppException
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)
zip_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
_task_manager = ZipTaskManager()


def _get_zip_extensions() -> set[str]:
    return set(get_config("allowed_zip_extensions", [".zip", ".tar.gz"]))


def _get_max_zip_size() -> int:
    return int(os.getenv("MAX_ZIP_SIZE", "524288000"))


@zip_router.post("/upload_zip")
async def upload_zip(
    file: UploadFile = File(...),
    user_id: str = Form("default_user"),
):
    filename = file.filename or ""
    allowed = _get_zip_extensions()
    if not any(filename.endswith(ext) for ext in allowed):
        raise AppException(
            message=f"不支持的压缩格式，支持：{', '.join(allowed)}",
            code=400,
        )

    tmp_dir = get_data_path("tmp")
    tmp_path = tmp_dir / f"upload_{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    content = await file.read()

    max_size_mb = _get_max_zip_size() // 1048576
    if len(content) > _get_max_zip_size():
        raise AppException(message=f"压缩包大小超过限制（最大 {max_size_mb}MB）", code=413)

    tmp_path.write_bytes(content)
    task_id = _task_manager.create_task(tmp_path, user_id)
    return success_response({
        "task_id": task_id, "status": "pending",
        "message": "压缩包已接收，正在后台处理",
    })


@zip_router.get("/task/{task_id}")
async def query_task(task_id: str):
    task = _task_manager.get_task(task_id)
    if not task:
        raise AppException(message="任务不存在", code=404)
    return success_response(task)


@zip_router.get("/task/{task_id}/stream")
async def stream_task_progress(task_id: str):
    """SSE 流式推送压缩包处理进度。前端连接后实时接收每文件处理结果。"""
    q = _task_manager.get_stream(task_id)
    if q is None:
        raise AppException(message="任务不存在或已过期", code=404)

    async def event_generator():
        try:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=600)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("event") == "done":
                    break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'event': 'error', 'data': '任务超时'})}\n\n"
        finally:
            _task_manager.cleanup_queue(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
