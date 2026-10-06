"""会话接口的请求/响应契约（§3.7.2）。

⚠️ §3.7.3 只给了会话接口的**路径与参数**，响应结构由 3.7.2 的表述定死 ——
   这里是那份表述的可执行版本。前端一律走 `openapi-typescript` 生成的
   `api/schema.d.ts` 取类型，不手抄（M4 的 pre-flight 裁决）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 标题长度与落库口径一致（`create_conversation` 里也是 [:50]）
TITLE_MAX = 50


class ConversationItem(BaseModel):
    id: str
    title: str
    # ⚠️ 库里的 `is_top` 是 0/1 整数（§3.7.2「is_top 为 0/1 布尔」）——
    #    这里照库返回，不转 bool，免得前端拿到 true 却拼不出 1 的语义
    is_top: int
    last_chat_time: str | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationItem]
    # `total` 与 `has_more` 都给是有意的（§3.7.2）：
    # total → 「共 N 条」；has_more → 滚动到底要不要再拉一页
    total: int
    has_more: bool


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=TITLE_MAX)


class PatchConversationRequest(BaseModel):
    """两个字段都可选，**至少给一个** —— 空请求由路由判 400。"""

    title: str | None = Field(default=None, max_length=TITLE_MAX)
    is_top: bool | None = None


class MessageItem(BaseModel):
    id: str
    role: str
    content: str
    # ⚠️ 落库的那一列原样返回（含 `[n]` 标记的正文 + 结构化引用）——
    #    刷新后重新渲染角标的**唯一**来源
    citations: list = []
    created_at: str | None = None


class MessagesResponse(BaseModel):
    messages: list[MessageItem]
