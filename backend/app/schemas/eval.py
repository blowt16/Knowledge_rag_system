"""评测接口的契约（§3.7.3 / §4.3.1.4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EvalRunRequest(BaseModel):
    name: str | None = None
    #: 省略/`full` → 全量；`refusal_calib` → 只跑校准小集（§10.2 M5-2）
    suite: str = "full"
    role: str = Field(default="student", pattern="^(student|staff|admin)$")
    #: 仅 admin 且**显式**打开才生效（对照实验要证明「admin 不是旁路」）
    include_restricted: bool = False
    #: 消融开关；空 = 完整链路
    config: dict[str, Any] | None = None
    #: 只跑指定的用例（调试用）；给了就忽略 suite 的筛选
    case_ids: list[str] | None = None


class EvalRunCreated(BaseModel):
    run_id: str
    status: str
    #: 服务端派生，前端直接用（§4.3.1.4）
    config_label: str


class EvalCaseResult(BaseModel):
    case_id: str
    retrieved_ids: list[str] = []
    unauthorized_hits: int = 0
    metrics: dict[str, Any] = {}


class EvalRunDetail(BaseModel):
    run_id: str
    name: str | None = None
    config: dict[str, Any] = {}
    config_label: str
    role: str | None = None
    status: str
    include_restricted: bool = False
    metrics: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cases: list[EvalCaseResult] = []


class EvalRunSummary(BaseModel):
    run_id: str
    name: str | None = None
    config: dict[str, Any] = {}
    config_label: str
    role: str | None = None
    status: str
    include_restricted: bool = False
    metrics: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EvalRunList(BaseModel):
    items: list[EvalRunSummary] = []


class EvalCompareRow(BaseModel):
    run_id: str
    config_label: str
    config: dict[str, Any] = {}
    status: str
    values: dict[str, Any] = {}


class EvalCompareResponse(BaseModel):
    #: 指标全集（固定顺序，表头不随数据乱跳）
    metrics: list[str] = []
    runs: list[EvalCompareRow] = []
