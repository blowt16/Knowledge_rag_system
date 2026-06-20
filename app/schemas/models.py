"""Pydantic 请求/响应数据模型。"""
from pydantic import BaseModel, Field
from typing import Optional


# ============================================================
# 对话相关
# ============================================================

class ChatRequest(BaseModel):
    query: str = Field(..., description="用户查询文本")
    session_id: Optional[str] = Field(None, description="会话 ID，新会话传 null")
    user_id: str = Field("default_user", description="用户标识")
    stream: bool = Field(True, description="是否流式返回")
    mode: str = Field("agent", description="模式：agent(Agent工具链) | rag(直接RAG检索) | auto(自动判定)")


# ============================================================
# 知识库管理
# ============================================================

class DocumentInfo(BaseModel):
    md5: str
    original_filename: str
    upload_time: str


class KnowledgeListResponse(BaseModel):
    user_id: str
    documents: list[DocumentInfo]
    total: int


# ============================================================
# 会话管理
# ============================================================

class ConversationCreateRequest(BaseModel):
    user_id: str = Field(..., description="用户标识")
    title: str = Field("", description="会话标题")


class ConversationInfo(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    conversations: list[ConversationInfo]


class MessageInfo(BaseModel):
    role: str
    content: str
    created_at: str


class ConversationMessagesResponse(BaseModel):
    session_id: str
    messages: list[MessageInfo]


# ============================================================
# 压缩包任务
# ============================================================

class TaskProgress(BaseModel):
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0


class TaskErrorDetail(BaseModel):
    file_path: str
    error_type: str
    reason: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: Optional[TaskProgress] = None
    error_details: Optional[list[TaskErrorDetail]] = None
    message: Optional[str] = None


# ============================================================
# 文档处理统一结果 — 全局公共复用文档管道
# ============================================================

class FileProcessResult(BaseModel):
    """单文件处理统一结果。两条上传链路（单文件/压缩包）均使用此结构。"""
    status: str  # "done" | "duplicate" | "failed"
    md5: str = ""
    filename: str = ""
    chunks: int = 0
    error_type: str = ""  # duplicate | empty_content | size_exceeded | parse_failed
    reason: str = ""
