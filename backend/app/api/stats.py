"""管理端统计与仪表盘接口（§3.7.3 仪表盘 / §4.4）。

**分层**（A7）：这里只做参数校验与响应封装，业务在 `services/stats_service.py`。

⚠️ `stats/retrieval` 在 Prometheus 不可用时返回 **`available: false` 且 HTTP 200**
   （M4-D5）—— §4.4 要求运行指标不可用时**业务指标区块照常渲染**，
   所以它不是错误，不能 500。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_role
from app.schemas.stats import (
    HotQuestionsResponse,
    OverviewResponse,
    RefusalStatsResponse,
    RetrievalMetricsResponse,
    TrendResponse,
)
from app.services import stats_service

router = APIRouter(prefix="/api/admin/stats", tags=["admin:stats"])

AdminUser = Depends(require_role("admin"))


@router.get("/overview", response_model=OverviewResponse)
async def overview(user=AdminUser) -> OverviewResponse:
    return OverviewResponse(**await stats_service.overview())


@router.get("/trend", response_model=TrendResponse)
async def trend(user=AdminUser, days: int = Query(30, ge=1, le=365)) -> TrendResponse:
    """近 N 天问答量 —— **按天补零**。"""
    return TrendResponse(**await stats_service.trend(days=days))


@router.get("/refusals", response_model=RefusalStatsResponse)
async def refusals(user=AdminUser) -> RefusalStatsResponse:
    """**仅聚合**（饼图 / Top N 计数），不含可标注的记录标识 —— 明细见
    `GET /api/admin/refusals`（§4.3.1.3）。"""
    return RefusalStatsResponse(**await stats_service.refusals_aggregate())


@router.get("/hot-questions", response_model=HotQuestionsResponse)
async def hot_questions(user=AdminUser, limit: int = Query(10, ge=1, le=50)
                        ) -> HotQuestionsResponse:
    return HotQuestionsResponse(**await stats_service.hot_questions(limit=limit))


@router.get("/retrieval", response_model=RetrievalMetricsResponse)
async def retrieval(user=AdminUser) -> RetrievalMetricsResponse:
    """★ 运行指标（PromQL 查 Prometheus）—— M4 里恒为 `available: false` + 200。"""
    return RetrievalMetricsResponse(**await stats_service.retrieval_metrics())
