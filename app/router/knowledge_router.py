"""知识库 REST API 路由。"""
from fastapi import APIRouter, UploadFile, File, Form, Query
from app.router.knowledge_service import KnowledgeService
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
