"""管理端文档接口的请求/响应契约（§3.7.3 / §4.3.1 / §4.3.2）。

⚠️ §3.7.3 只给了**路径与参数**，响应结构由 §4.3.1 的页面表格（列名）与
   §4.3.2 的字段表反推 —— 这里是那份口径的可执行版本。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocStatus = Literal["indexing", "active", "disabled", "failed"]
Visibility = Literal["public", "restricted"]


class DocumentItem(BaseModel):
    id: str
    doc_group_id: str
    title: str
    filename: str
    file_type: str
    version: int
    status: str
    visibility: str
    visible_roles: list[str] = []
    effective_date: str | None = None
    chunk_count: int = 0
    created_at: str | None = None
    # 「这一版是不是它所在组的当前生效版本」—— 版本管理页要高亮它（§4.3.1）
    is_current: bool = False


class DocumentListResponse(BaseModel):
    items: list[DocumentItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class VersionListResponse(BaseModel):
    doc_group_id: str
    versions: list[DocumentItem]


class ChunkItem(BaseModel):
    chunk_index: int
    page: int
    current_chapter: str = ""
    chapter_level: int = 0
    char_start: int = 0
    char_end: int = 0
    text: str = ""
    image_paths: list[str] = []


class ChunkListResponse(BaseModel):
    chunks: list[ChunkItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class PatchDocumentRequest(BaseModel):
    """可改字段（§3.7.2 侧的管理端写接口规格）。

    ⚠️ `extra="forbid"`：`file_type` / `md5` / `version` / `doc_group_id`
       **不可改** —— 要换内容请传新版本。静默忽略未知字段会让管理员
       以为改成功了，所以宁可报 422。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    visibility: Visibility | None = None
    visible_roles: list[str] | None = None
    effective_date: str | None = None
    status: DocStatus | None = None
