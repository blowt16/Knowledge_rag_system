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


class RetrievalMetricsResponse(BaseModel):
    available: bool
    status: str | None = None
    latency_p50: float | None = None
    latency_p95: float | None = None
    error_rate: float | None = None
    token_usage: float | None = None
