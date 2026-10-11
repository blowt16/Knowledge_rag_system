"""仪表盘接口的响应契约（§3.7.3 / §4.4）。

⚠️ `RetrievalMetricsResponse` 的 `available` 是**判别字段**：
   `false` 时只保证有 `status`，其余指标可以为 `None` ——
   前端据此决定「运行指标区块」显示「运行指标暂不可用」，
   而**业务指标区块照常渲染**（§4.4）。
"""

from __future__ import annotations

from pydantic import BaseModel


class OverviewResponse(BaseModel):
    document_count: int
    chunk_count: int
    qa_count: int
    refusal_rate: float
    # `{kind: count}`，数据源 `degradation_events`（读 PostgreSQL）
    degradation_counts: dict[str, int] = {}


class TrendDay(BaseModel):
    date: str
    qa_count: int
    refusal_count: int


class TrendResponse(BaseModel):
    days: list[TrendDay]


class ReasonCount(BaseModel):
    reason: str
    count: int


class QuestionCount(BaseModel):
    question: str
    count: int


class RefusalStatsResponse(BaseModel):
    by_reason: list[ReasonCount] = []
    top_questions: list[QuestionCount] = []


class HotQuestionsResponse(BaseModel):
    items: list[QuestionCount] = []


class AlertRuleState(BaseModel):
    name: str
    #: inactive / pending / firing —— 前端「有 firing 即标红」（§4.4）
    state: str


class AlertStatus(BaseModel):
    firing: int
    pending: int
    rules: list[AlertRuleState] = []


class NodeLatency(BaseModel):
    node: str
    p95: float


class RetrievalMetricsResponse(BaseModel):
    available: bool
    status: str | None = None
    latency_p50: float | None = None
    latency_p95: float | None = None
    error_rate: float | None = None
    token_usage: float | None = None
    #: LLM 成本（【方案 §8.2】新增，改造前完全没有）
    cost: float | None = None
    #: token 拆输入/输出（【方案 §8.2】新增；改造前只有一个合计）
    #: ⚠️ 数据来自**三个独立 measure**（`inputTokens`/`outputTokens`/`totalTokens`），
    #:    **不是** `usageType` 维度 —— 实测那个维度返回的三行是同一个总量的副本，
    #:    既拆不开、全加还会报 3 倍（Task 10 踩过两次）
    token_input: float | None = None
    token_output: float | None = None
    #: 按节点的 p95 耗时（§8.2 新增；把 §4.4 表里「按节点分组」那条真正做出来）
    node_latency: list[NodeLatency] = []
    #: 告警状态（**后端自算**，【方案 §8.3】方案 A）—— 读不到时为 None
    alerts: AlertStatus | None = None
