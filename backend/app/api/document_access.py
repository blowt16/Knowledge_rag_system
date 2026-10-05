"""User 端原文访问（§3.7.2 / §3.6）。

三条接口，一条铁律：**必须走 `filters.can_access`**（与检索同一套行级 ACL）。
`/api/documents/{id}/file` 是一条绕过检索期 ACL 的**捷径** —— 搜不到不等于下不到。
若这里另写一套判定，两套迟早不一致，「检索期数据隔离」就被这条捷径架空。

⚠️ 不可见 → **404 而不是 403**：403 等于告诉对方「这份文档存在，只是你没权限」，
   可以被用来探测文档是否存在。**「不存在」与「不可见」必须不可区分**。

⚠️ 提权（`include_restricted=true`，**仅 admin**）→ 200 + `escalated: true` + 审计留痕。
   非 admin 传了按未传处理（不报错、不记提权）—— 不用错误响应泄露角色差异。

⚠️ 图片走**短期签名 URL**（默认 5 分钟）：`<img src>` 带不上 Authorization 头，
   而把 JWT 塞进查询串会让 token 进访问日志与浏览器历史 ——
   与「身份只能来自 JWT」冲突。签名只覆盖「文档 + 文件名 + 过期时间」，
   所以它**不能**被换成别的文档或别的图。

⚠️ 分层（A7）：本文件只做参数校验与响应封装，可见性判定统一走 `filters.py`。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app import db
from app.core.config import data_dir, secret
from app.core.deps import CurrentUser, OptionalUser
from app.core.exceptions import Forbidden, NotFound, Unauthorized
from app.core.telemetry import current_trace_id
from app.retrieval import filters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

# 签名 URL 有效期（§3.7.2：默认 5 分钟）
SIGNED_URL_TTL_SECONDS = 300

# 图片名只允许这些字符 —— 挡掉 `../` 这类路径穿越
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


# ---- 可见性（唯一入口）--------------------------------------------------

async def _visible_document(document_id: str, user, include_restricted: bool) -> dict:
    """过 `filters.can_access`，并返回 documents 行。

    ⚠️ **不存在**与**不可见**返回同一个 404 —— 两者不可区分（§3.7.2）。
    """
    async with db.tx() as conn:
        decision = await filters.can_access(
            conn, document_id, user, include_restricted=include_restricted,
        )
        if not decision.allowed:
            raise NotFound("文档不存在")
        row = await conn.fetchrow(
            """SELECT id, filename, file_type, title, source_path, normalized_text_path
                 FROM documents WHERE id = $1""",
            document_id,
        )
    if row is None:                      # 理论上不可达：can_access 已判过存在性
        raise NotFound("文档不存在")
    return {**dict(row), "escalated": decision.escalated}


def _audit_escalation(user, document_id: str, action: str) -> None:
    """提权必须留痕：谁、何时、看了哪份文档（§3.3.3）。

    落在**结构化日志**里（方案三处都写的是「审计日志」；12 张表里没有审计表，
    新建表等于动 M0 冻结的 schema）。trace_id 一起记，方便与那次问答对上。
    """
    logger.warning("提权访问原文", extra={
        "event": "document_access.escalated",
        "user_id": user.id,
        "user_name": user.username,
        "user_role": user.role,
        "document_id": document_id,
        "action": action,
        "trace_id": current_trace_id(),
    })


# ---- 原始文件 -----------------------------------------------------------

@router.get("/{document_id}/file")
async def get_file(document_id: str, user: CurrentUser, include_restricted: bool = False):
    """原始文件（PDF 用 pdf.js 文本层定位；pptx 本轮降级为下载）。

    ⚠️ 提权成功时用响应头 `X-Escalated: true` 标注 —— 这是二进制流，
       没有地方放响应体的字段。
    """
    doc = await _visible_document(document_id, user, include_restricted)

    path = _safe_path(doc["source_path"])
    if path is None or not path.exists():
        raise NotFound("文档不存在")

    if doc["escalated"]:
        _audit_escalation(user, document_id, "file")

    headers = {"X-Escalated": "true"} if doc["escalated"] else {}
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(doc["file_type"], "application/octet-stream"),
        filename=doc["filename"],
        headers=headers,
    )


# ---- 规范化文本 ---------------------------------------------------------

@router.get("/{document_id}/text")
async def get_text(document_id: str, user: CurrentUser, include_restricted: bool = False):
    """规范化文本 + 偏移索引。

    `char_offset_index` 是 `[{chunk_id, char_start, char_end, page}]`，
    用于把偏移映射回 chunk（§3.7.2）。

    ⚠️ 本轮**没有前端消费方**（原文回跳只调 `/file`）—— 接口按契约实现，
       但别指望在页面上看到它被用到。
    """
    doc = await _visible_document(document_id, user, include_restricted)

    path = _safe_path(doc["normalized_text_path"])
    if path is None or not path.exists():
        raise NotFound("文档不存在")

    if doc["escalated"]:
        _audit_escalation(user, document_id, "text")

    text = path.read_text(encoding="utf-8")
    return {
        "document_id": document_id,
        "text": text,
        "char_offset_index": await _char_offset_index(document_id),
    }


async def _char_offset_index(document_id: str) -> list[dict]:
    """从向量库的 metadata 取分块边界（唯一权威，别处没有这份数据）。

    ⚠️ `collection.get` **不保证返回顺序**（M1 的 B-2 教训），所以自己按
       `chunk_index` 排 —— 让中间层的返回顺序决定最终顺序，是踩过的坑。
    """
    from app.retrieval.vector import get_collection

    data = get_collection().get(where={"document_id": {"$eq": document_id}},
                                include=["metadatas"])
    items = []
    for meta in data.get("metadatas") or []:
        items.append({
            "chunk_id": meta.get("chunk_id"),
            "char_start": meta.get("char_start"),
            "char_end": meta.get("char_end"),
            "page": meta.get("page"),
            "chunk_index": meta.get("chunk_index"),
        })
    items.sort(key=lambda x: x["chunk_index"] if x["chunk_index"] is not None else 0)
    return items


# ---- 图片（短期签名 URL）------------------------------------------------

@router.get("/{document_id}/images/{name}")
async def get_image(document_id: str, name: str, request: Request, user: OptionalUser,
                    include_restricted: bool = False):
    """带 JWT 调 → 返回签名 URL；带签名调（无 JWT）→ 返回图片本身。

    ⚠️ `/images` 静态挂载**已删除**（main.py）—— 把图目录直接对外裸露，
       等于绕过 ACL 把受限文档的图交出去。
    """
    if not _SAFE_NAME.match(name or ""):
        raise NotFound("图片不存在")

    # ① 带签名：自证身份，不再要求 JWT（<img src> 带不上请求头）
    exp, sig = request.query_params.get("exp"), request.query_params.get("sig")
    if sig:
        if not _verify(document_id, name, exp, sig):
            raise Forbidden("链接已失效，请重新打开引用")
        return _image_response(document_id, name)

    # ② 带 JWT：走同一套 ACL，换取签名 URL
    if user is None:
        raise Unauthorized("缺少访问令牌")
    doc = await _visible_document(document_id, user, include_restricted)
    if doc["escalated"]:
        _audit_escalation(user, document_id, "images")

    path = _image_path(document_id, name)
    if path is None or not path.exists():
        raise NotFound("图片不存在")

    expires_at = int(time.time()) + SIGNED_URL_TTL_SECONDS
    signature = _sign(document_id, name, expires_at)
    return {
        "url": f"/api/documents/{document_id}/images/{name}"
               f"?exp={expires_at}&sig={signature}",
        "expires_at": expires_at,
    }


def _image_response(document_id: str, name: str):
    path = _image_path(document_id, name)
    if path is None or not path.exists():
        raise NotFound("图片不存在")
    return FileResponse(path)


# ---- 签名 --------------------------------------------------------------

def _sign(document_id: str, name: str, exp: int | str) -> str:
    """签名覆盖「文档 + 文件名 + 过期时间」—— 换其中任何一个都过不了校验。"""
    message = f"{document_id}|{name}|{exp}".encode()
    return hmac.new(secret("auth.jwt_secret").encode(), message,
                    hashlib.sha256).hexdigest()


def _verify(document_id: str, name: str, exp: str | None, sig: str) -> bool:
    try:
        if exp is None or int(exp) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(_sign(document_id, name, exp), sig)


# ---- 路径 --------------------------------------------------------------

def _safe_path(raw: str | None) -> Path | None:
    """库里的路径是入库时写下的绝对路径；文件没了一起当 404。"""
    if not raw:
        return None
    try:
        return Path(raw)
    except (TypeError, ValueError):
        return None


def _image_path(document_id: str, name: str) -> Path | None:
    if not _SAFE_NAME.match(name or ""):
        return None
    return data_dir() / "extracted_images" / document_id / name
