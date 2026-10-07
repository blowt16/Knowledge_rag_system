"""RAGState 定义（§3.5.1）。

⚠️ 状态每轮**新建**，不是跨轮复用 —— 上一轮的 `refused` / `verify_report` /
   `citations` 必须为空，否则前端可能收到上一轮的事件。

⚠️ 跨轮保留的只有三样：`session_id`、从 `messages` 加载的 `history`、
   以及 `last_route`（读自 `messages.route`）。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from app.schemas.chat import Citation, VerifyReport

RouteName = Literal["chat", "clarify", "knowledge"]
QuerySource = Literal["verbatim", "keywords", "hyde"]
QueryTarget = Literal["bm25", "vector", "both"]


@dataclass
class UserContextLite:
    """图内使用的用户身份（从 JWT 来，不含敏感字段）。"""
    id: str
    role: str


@dataclass
class Message:
    role: str      # user | assistant
    content: str


class NodeTrace(TypedDict):
    node: str
    ms: int
    recalled: int
    # 降级类型，未降级为 None（与 degradation_events.kind 同词表）
    degraded: str | None


class RetrievalQuery(TypedDict):
    text: str
    target: QueryTarget
    weight: float
    source: QuerySource


@dataclass
class Chunk:
    """检索候选。`reranked` 不能替代 `evidence` —— 见下。"""
    chunk_id: str
    text: str
    score: float = 0.0
    document_id: str = ""
    doc_group_id: str = ""
    version: int = 0
    document_name: str = ""
    chunk_index: int = 0
    page: int = 1
    char_start: int = 0
    char_end: int = 0
    chapter: str = ""
    chapter_level: int = 0
    images: list[str] = field(default_factory=list)
    bbox: list[dict] = field(default_factory=list)
    escalated: bool = False


class RAGState(TypedDict, total=False):
    # ---- 输入 ----
    query: str
    session_id: str
    user: UserContextLite
    include_restricted: bool
    # 消融实验的开关（§5.3 / M5-3）。**线上恒为空 dict** —— 空就是默认行为，
    # 节点只在看到显式开关时才改变行为（见 `app.eval.config` 的取值校验）。
    eval_config: dict

    # ---- 查询理解 ----
    history: list[Message]
    resolved_query: str
    # none | gate | no_history | no_feature | timeout
    resolve_skipped_reason: str
    resolve_unresolved: bool
    route: RouteName
    route_source: str          # rule | llm
    last_route: str
    # 澄清轮次上限的计数（从 messages 数出来，见 chat_service._clarify_counts）
    clarify_chain: int          # 紧邻本轮的连续澄清轮次
    clarify_total: int          # 本会话累计澄清次数
    clarify_skipped: bool       # 本轮因到顶而跳过澄清
    clarify_question: str
    clarify_facets: list[str]

    # ---- 检索 ----
    retrieval_queries: list[RetrievalQuery]
    candidates: list[Chunk]
    reranked: list[Chunk]
    # ★ 编号清单 —— 顺序即 prompt 里的 [1]…[N]
    #   `reranked` **不能替代它**：build_context 是「按文档分组 → 组内按
    #   chunk_index 排序」后才拼 context，**prompt 里的顺序 ≠ reranked 的顺序**。
    #   拿 reranked 做映射，[1] 会指向错的 chunk —— 引用错位比漏引用更糟。
    evidence: list[Chunk]
    retrieval_confidence: float | None
    rerank_degraded: bool

    # ---- 生成 ----
    context: str
    # 滚动压缩产出的历史摘要（§3.8.3）—— 拼 prompt 时排在历史**之前**，
    # 顺序见 §3.8.5：[系统][摘要][messages[compressed_count:]][检索上下文][本轮问题]
    summary: str
    answer: str
    # ★ ANSWERED | REFUSED_NO_EVIDENCE —— 条件边读它
    decision: str
    citations: list[Citation]
    verify_report: VerifyReport
    refused: bool
    refusal_reason: str

    # ---- 可观测 ----
    # 用 operator.add reducer：TypedDict 下节点返回 trace 是**覆盖**语义，
    # 不写 reducer 的话只会留下最后一个节点的记录。
    trace: Annotated[list[NodeTrace], operator.add]


def new_state(**overrides: Any) -> RAGState:
    """每轮新建状态 —— 不要跨轮复用。"""
    base: RAGState = {
        "session_id": "",
        "history": [],
        "resolved_query": "",
        "resolve_skipped_reason": "none",
        "resolve_unresolved": False,
        "route": "knowledge",
        "route_source": "rule",
        "last_route": "",
        "clarify_question": "",
        "clarify_facets": [],
        "retrieval_queries": [],
        "candidates": [],
        "reranked": [],
        "evidence": [],
        "retrieval_confidence": None,
        "rerank_degraded": False,
        "context": "",
        "summary": "",
        "answer": "",
        "decision": "",
        "citations": [],
        "verify_report": VerifyReport(),
        "refused": False,
        "refusal_reason": "",
        "trace": [],
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base
