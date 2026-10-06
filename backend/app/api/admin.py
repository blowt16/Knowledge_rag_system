"""管理端：文档（§3.7.3 / §4.3.1 / §4.3.2）+ 拒答分析（§4.3.1.3）。

**分层**（A7）：这里只做参数校验与响应封装，业务在 `services/document_service.py`。

⚠️ 上传的两条路由仍在 `api/documents.py`（同一个 `/api/admin/documents` 前缀，
   两处不冲突：段数不同）。用户在 `main.py` 里把本模块**注册在它之后**。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_role
from app.core.exceptions import AppError
from app.schemas.refusal import (
    AnnotateRequest,
    AnnotateResponse,
    AnnotationItem,
    RefusalListResponse,
)
from app.schemas.document import (
    ChunkListResponse,
    DocumentItem,
    DocumentListResponse,
    PatchDocumentRequest,
    VersionListResponse,
)
from app.services import document_service as docs
from app.services import qa_log_service as qa_logs

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 与 `api/documents.py` 同一个写法：
# ⚠️ 必须当**默认值**用（`user=AdminUser`），不能当注解（`user: AdminUser`）——
#    裸 `Depends(...)` 不是类型，Pydantic 生成 schema 时会炸；
#    而 `Annotated[...]` 别名（如 `CurrentUser`）反过来只能当注解用（M3 的 D-9）。
AdminUser = Depends(require_role("admin"))


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    user=AdminUser,
    status: str = Query("all", pattern="^(all|indexing|active|disabled|failed)$"),
    visibility: str = Query("all", pattern="^(all|public|restricted)$"),
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    # 全站分页上限统一 100（§4.3.1.1）—— 超了直接 422，不悄悄截断
    page_size: int = Query(20, ge=1, le=100),
) -> DocumentListResponse:
    return DocumentListResponse(**await docs.list_documents(
        status=status, visibility=visibility, q=q, page=page, page_size=page_size))


@router.get("/documents/{group_id}/versions", response_model=VersionListResponse)
async def list_versions(group_id: str, user=AdminUser) -> VersionListResponse:
    return VersionListResponse(**await docs.get_versions(group_id))


@router.get("/documents/{document_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    document_id: str,
    user=AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ChunkListResponse:
    return ChunkListResponse(**await docs.get_chunks_preview(
        document_id, page=page, page_size=page_size))


@router.patch("/documents/{document_id}", response_model=DocumentItem)
async def patch_document(
    document_id: str, body: PatchDocumentRequest, user=AdminUser
) -> DocumentItem:
    """改可改字段。

    ⚠️ `visibility` / `visible_roles` / `effective_date` / `status` 会**触发重索引**
       （它们冗余在 Chroma 里，§4.3.1.2）—— 前端必须**显式告知耗时并给进度**，
       不能做成静默的即时保存（那是前端的活，见 3b）。
    """
    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise AppError("请求体至少要给一个可改字段")
    return DocumentItem(**await docs.update_document(document_id, **provided))


@router.post("/documents/{document_id}/disable", response_model=DocumentItem)
async def disable_document(document_id: str, user=AdminUser) -> DocumentItem:
    return DocumentItem(**await docs.set_document_status(document_id, "disabled"))


@router.post("/documents/{document_id}/enable", response_model=DocumentItem)
async def enable_document(document_id: str, user=AdminUser) -> DocumentItem:
    return DocumentItem(**await docs.set_document_status(document_id, "active"))


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, user=AdminUser) -> dict:
    await docs.delete_document(document_id)
    return {"message": "已删除"}


# ============================================================
# 拒答分析（§4.3.1.3）
# ============================================================

@router.get("/refusals", response_model=RefusalListResponse)
async def list_refusals(
    user=AdminUser,
    kind: str = Query("refused", pattern="^(refused|clarify|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> RefusalListResponse:
    """**明细**（含可标注的 `qa_logs.id`）—— 聚合看 `stats/refusals`。

    `kind` 默认 `refused`（向后兼容）；`clarify` 列被反问的轮次，
    `all` 两者都列、行上带 `kind`。
    """
    return RefusalListResponse(**await qa_logs.list_refusals(
        page=page, page_size=page_size, kind=kind))


@router.post("/refusals/{log_id}/annotate", response_model=AnnotateResponse)
async def annotate_refusal(log_id: str, body: AnnotateRequest, user=AdminUser
                           ) -> AnnotateResponse:
    if body.suggested_document_id is None and body.note is None:
        # 两个字段至少填一个（§4.3.1.3）
        raise AppError("建议补充的文档与备注至少要填一个")
    return AnnotateResponse(annotation=AnnotationItem(
        **await qa_logs.annotate_refusal(
            log_id, suggested_document_id=body.suggested_document_id,
            note=body.note, annotated_by=user.id)))
