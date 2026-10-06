"""问答与 SSE 契约（§3.7.2 / §3.5.3 节点 10）。

⚠️ M0 要「钉死 SSE 协议」**必须包含载荷 schema**，不只是事件名：
   原表述只说了「事件表」，而 `Citation` / `VerifyReport` 的字段级结构在节点 10。
   若 M0 只冻结事件名、任简实现自造载荷，前端照它写，
   M3 引用统一时结构一变就要返工 —— 正是「钉死协议」想避免的事。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RouteName = Literal["chat", "clarify", "knowledge"]
RefusalReason = Literal["no_candidate", "insufficient_evidence"]
Decision = Literal["ANSWERED", "REFUSED_NO_EVIDENCE"]
SseErrorCode = Literal[
    "timeout", "upstream_error", "context_length_exceeded", "internal", "session_busy",
]
StageName = Literal[
    "routing", "resolving", "retrieving", "reranking", "generating", "verifying",
]


# ---- 引用 --------------------------------------------------------------

class Box(BaseModel):
    """bbox 坐标框 —— **引用回跳的主定位依据**（§4.2.2.5）。

    来源是 chunk metadata 的 `bbox` 字段，支持跨页框。
    非 PDF、扫描件、旧索引会为空数组，前端落到 L2 文本匹配。
    """
    page: int
    x0: float
    x1: float
    top: float
    bottom: float


class JumpTarget(BaseModel):
    """原文回跳定位（§4.2.2.5）。"""
    # documents 表主键 —— 调 /api/documents/{id}/file 需要它。
    # Citation 顶层只有 document_name（供展示），两者不可互相替代。
    document_id: str
    page: int
    # 相对**规范化文本**的偏移，Unicode 码点
    char_start: int
    char_end: int
    boxes: list[Box] = Field(default_factory=list)


class Citation(BaseModel):
    """引用条目。"""
    marker: int                       # 正文里的 [n]
    document_name: str
    chapter: str = ""
    page: int = 1
    snippet: str = ""
    chunk_id: str
    # 该引用是否属于「越权取得」（admin 以 include_restricted=true 检索时）
    # —— 5.3 的 ACL 对照实验直接读它
    escalated: bool = False
    # ⚠️ 存**文件名**，不是签名 URL：
    #    /images/{name} 返回 5 分钟过期的短期签名 URL，而 citations 会**落库**、
    #    用于刷新后重新渲染引用 —— 存 URL 的话刷新历史会话时图片全部裂图。
    #    前端每次渲染时按 name 现取签名 URL。
    images: list[str] = Field(default_factory=list)
    jump_target: JumpTarget | None = None


# ---- 后校验 ------------------------------------------------------------

class InvalidMarker(BaseModel):
    marker: int
    char_start: int
    char_end: int


class UncitedClaim(BaseModel):
    # ⚠️ sentence 不是冗余字段：它是前端「校验 A」的输入
    #    （rawText[char_start:char_end] === sentence），不可省略、不可裁剪
    sentence: str
    char_start: int
    char_end: int


class VerifyReport(BaseModel):
    """声明级校验报告（§3.5.3 节点 10）。

    它是**唯一能自证的幻觉指标** ——「无依据结论占比」可直接量化，
    进仪表盘、进评测表。

    偏移相对**渲染前的原始答案文本**，单位 **Unicode 码点**。
    """
    total_claims: int = 0
    cited_claims: int = 0
    invalid_markers: list[InvalidMarker] = Field(default_factory=list)
    uncited_claims: list[UncitedClaim] = Field(default_factory=list)


# ---- 请求 --------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    # 不传即新建会话；传则续接已有会话
    session_id: str | None = None
    # 客户端生成，用于同会话幂等去重（防双击重复提交）
    request_id: str = Field(min_length=1, max_length=64)
    # 仅 admin 可传；非 admin 传了按 false 处理（不报错、不记提权）
    include_restricted: bool | None = False

    # ⚠️ 请求体里**没有** user_id —— 一律从 JWT 取（附录 A 第 2 条）


# ---- SSE 事件载荷（供前端类型生成与测试用）---------------------------

class SessionCreatedEvent(BaseModel):
    session_id: str


class ResolvedEvent(BaseModel):
    resolved_query: str


class ClarifySkippedEvent(BaseModel):
    """澄清因**轮次到顶**被跳过时下发（追加事件，不改既有事件）。"""

    text: str


class RouteEvent(BaseModel):
    route: RouteName
    # ⚠️ 字段名是 `clarify_facets`（不是 `facets`）——
    #    写成 evt.facets 会恒为 undefined，澄清选项永远渲染不出来
    clarify_facets: list[str] | None = None


class StageEvent(BaseModel):
    stage: StageName
    # ⚠️ label 只作兜底与调试，前端展示文案**以 stage 为准** ——
    #    两个文案源并存会造成不一致
    label: str = ""


class DecisionEvent(BaseModel):
    decision: Decision


class TokenEvent(BaseModel):
    # ⚠️ 协议级硬约束：必须是 JSON 解码后的纯文本，不得残留转义。
    #    客户端按序拼接的结果必须**逐字等于**服务端校验偏移时使用的文本 ——
    #    否则前端的置灰映射会静默错位，且无法靠任何校验发现。
    text: str


class CitationsEvent(BaseModel):
    citations: list[Citation]


class RefusedEvent(BaseModel):
    reason: RefusalReason
    # 文案的唯一来源是服务端；前端**不得**按 reason 自造文案 ——
    # 否则用户当场看到的（前端硬编码）与刷新后从 messages 读回的（服务端入库）不一致
    text: str
    hint: str | None = None


class DoneEvent(BaseModel):
    latency_ms: int


class ErrorEvent(BaseModel):
    code: SseErrorCode
    message: str
