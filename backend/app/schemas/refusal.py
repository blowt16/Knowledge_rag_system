"""拒答明细与标注的契约（§4.3.1.3）。

⚠️ 为什么明细是独立接口：`stats/refusals` 是**聚合**（饼图 / Top N 计数），
   **不含可标注的记录标识** —— 要标注就得有 `qa_logs.id`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AnnotationItem(BaseModel):
    suggested_document_id: str | None = None
    note: str | None = None
    annotated_by: str | None = None
    updated_at: str | None = None


class RefusalItem(BaseModel):
    """§4.3.1.3 的字段表：id / question / refusal_reason / created_at / annotation。

    `annotation` 未标注时是 **null**（不是空对象）—— 前端据此判断「还没标」。
    """

    id: str
    question: str
    refusal_reason: str | None = None
    # 行类型：`refused`（拒答）/ `clarify`（被反问）—— 见 §9.9 E2E-B1
    kind: str = "refused"
    # 本轮是否因澄清轮次到顶而跳过反问
    clarify_skipped: bool = False
    created_at: str | None = None
    annotation: AnnotationItem | None = None


class RefusalListResponse(BaseModel):
    items: list[RefusalItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class AnnotateRequest(BaseModel):
    """两个字段**至少填一个**，否则 400（§4.3.1.3）。"""

    model_config = ConfigDict(extra="forbid")

    suggested_document_id: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class AnnotateResponse(BaseModel):
    annotation: AnnotationItem
