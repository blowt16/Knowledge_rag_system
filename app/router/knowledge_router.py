"""知识库 REST API 路由。"""
import json
import asyncio

from fastapi import APIRouter, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from app.config.loader import get_config
from app.router.knowledge_service import KnowledgeService
from app.rag.single_upload_tracker import get_single_upload_tracker
from app.core.success_response import success_response
from app.core.failed_response import AppException

knowledge_router = APIRouter(prefix="/knowledge", tags=["knowledge"])
_svc = KnowledgeService()


@knowledge_router.post("/add/single")
async def upload_single(
    file: UploadFile = File(...),
    user_id: str = Form("default_user"),
):
    """上传单个文档到知识库。"""
    file_bytes = await file.read()
    filename = file.filename or "unknown"

    validation = _svc.validate_file(file_bytes, filename)
    if not validation["valid"]:
        raise AppException(message=validation["error"], code=400)

    result = await _svc.upload_single(file_bytes, filename, user_id)

    if result.get("status") == "duplicate":
        return success_response(result, "文件已存在，跳过处理")
    elif result.get("status") == "failed":
        diagnosis = result.get("diagnosis", {})
        if diagnosis:
            raise AppException(
                message=diagnosis.get("detail", "文档处理失败"),
                code=400,
                detail=str(diagnosis),
            )
        return success_response(result, "文档处理失败", code=400)

    return success_response(result, "文档上传处理成功")


@knowledge_router.get("/documents")
async def list_documents(user_id: str = Query("default_user")):
    """获取用户知识库文档列表。"""
    documents = _svc.get_documents(user_id)
    return success_response({
        "user_id": user_id,
        "documents": documents,
        "total": len(documents),
    })


@knowledge_router.delete("/md5/delete/{md5}")
async def delete_by_md5(md5: str, user_id: str = Query("default_user")):
    """按 MD5 删除文档（三层联动）。"""
    _svc.delete_by_md5(user_id, md5)
    return success_response(None, "文档已删除")


@knowledge_router.delete("/md5/clear")
async def clear_user_knowledge(user_id: str = Query("default_user")):
    """清空用户知识库。"""
    _svc.clear_user(user_id)
    return success_response(None, "知识库已清空")


@knowledge_router.delete("/md5/{filename}")
async def delete_by_filename(filename: str, user_id: str = Query("default_user")):
    """按文件名删除文档。"""
    documents = _svc.get_documents(user_id)
    deleted = 0
    for doc in documents:
        if doc.get("original_filename") == filename:
            _svc.delete_by_md5(user_id, doc.get("md5", ""))
            deleted += 1
    if deleted == 0:
        raise AppException(message=f"未找到文件: {filename}", code=404)
    return success_response({"deleted": deleted}, f"已删除 {deleted} 个文档")


# ============================================================
# 单文件上传（SSE 流式进度）
# ============================================================
_tracker = get_single_upload_tracker()


@knowledge_router.post("/single/upload")
async def upload_single_stream(
    file: UploadFile = File(...),
    user_id: str = Form("default_user"),
):
    """上传单个文档（异步后台处理 + SSE 进度），立即返回 task_id。"""
    file_bytes = await file.read()
    filename = file.filename or "unknown"

    validation = _svc.validate_file(file_bytes, filename)
    if not validation["valid"]:
        raise AppException(message=validation["error"], code=400)

    task_id = _tracker.create_task(file_bytes, filename, user_id)
    return success_response({
        "task_id": task_id,
        "status": "pending",
        "message": "文件已接收，正在后台处理",
    })


@knowledge_router.get("/single/task/{task_id}/stream")
async def stream_single_progress(task_id: str):
    """SSE 流式推送单文件处理进度。"""
    q = _tracker.get_stream(task_id)
    if q is None:
        raise AppException(message="任务不存在或已过期", code=404)

    async def event_generator():
        try:
            while True:
                timeout = int(get_config("sse_stream_timeout", 600))
                event = await asyncio.wait_for(q.get(), timeout=timeout)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("event") in ("done",):
                    break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'event': 'error', 'data': '任务超时'})}\n\n"
        finally:
            _tracker.cleanup(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
