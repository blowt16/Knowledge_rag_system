"""压缩包上传与任务查询路由。"""
import uuid
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form
from app.rag.zip_handler.zip_handler import ZipTaskManager
from app.core.success_response import success_response
from app.core.failed_response import AppException
from app.utils.path_tool import get_data_path

zip_router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
_task_manager = ZipTaskManager()

ALLOW_ZIP_EXTENSIONS = {".zip", ".tar.gz", ".rar"}
MAX_ZIP_SIZE = 500 * 1024 * 1024  # 500MB


@zip_router.post("/upload_zip")
async def upload_zip(
    file: UploadFile = File(...),
    user_id: str = Form("default_user"),
):
    """上传压缩包，后台异步处理。"""
    # 扩展名校验
    filename = file.filename or ""
    if not any(filename.endswith(ext) for ext in ALLOW_ZIP_EXTENSIONS):
        raise AppException(
            message=f"不支持的压缩格式，支持：{', '.join(ALLOW_ZIP_EXTENSIONS)}",
            code=400,
        )

    # 保存临时文件
    tmp_dir = get_data_path("tmp")
    tmp_path = tmp_dir / f"upload_{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    content = await file.read()

    if len(content) > MAX_ZIP_SIZE:
        raise AppException(message="压缩包大小超过限制（最大 500MB）", code=413)

    tmp_path.write_bytes(content)

    task_id = _task_manager.create_task(tmp_path, user_id)
    return success_response({
        "task_id": task_id,
        "status": "pending",
        "message": "压缩包已接收，正在后台处理",
    })


@zip_router.get("/task/{task_id}")
async def query_task(task_id: str):
    """查询压缩包处理任务状态。"""
    task = _task_manager.get_task(task_id)
    if not task:
        raise AppException(message="任务不存在", code=404)
    return success_response(task)
