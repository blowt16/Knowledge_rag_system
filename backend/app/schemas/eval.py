"""评测接口的契约（§3.7.3 / §4.3.1.4 / §5）。

⚠️ **新字段必须同步加进 schema。** FastAPI 会**静默丢弃**响应模型里没声明的
   字段 —— 不报错、不警告，前端就是收不到。报告弹窗整个是空的，
   而接口看着完全正常。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EvalRunRequest(BaseModel):
    name: str | None = None
    #: 省略/`full` → 全量；`refusal_calib` → 只跑校准小集（§10.2 M5-2）
    suite: str = "full"
    #: **批量评测页走这条**：只跑这个集里 `in_eval=true` 的用例（§5.5）。
    #: 给了它就别带 `config` —— 带了 config 这一轮会被判成消融运行（§5.4）。
    set_id: str | None = None
    role: str = Field(default="student", pattern="^(student|staff|admin)$")
    #: 仅 admin 且**显式**打开才生效（对照实验要证明「admin 不是旁路」）
    include_restricted: bool = False
    #: 消融开关；**空 = 走线上完整链路**（§5.4）
    config: dict[str, Any] | None = None
    #: 只跑指定的用例（调试用）；给了就忽略 suite 的筛选
    case_ids: list[str] | None = None


class EvalRunCreated(BaseModel):
    run_id: str
    status: str
    #: 服务端派生，前端直接用（§4.3.1.4）
    config_label: str


class EvalCaseResult(BaseModel):
    #: ⚠️ **可空**：用例删了，历史那行还在，只是不再指向某个用例
    #: （外键 ON DELETE SET NULL）。写成必填的话，任何「用例被删过」的历史 run
    #: 都会因响应校验失败 **500**。
    case_id: str | None = None
    retrieved_ids: list[str] = []
    unauthorized_hits: int = 0
    metrics: dict[str, Any] = {}


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
    #: 评测集名字的**快照** —— 评测集删了，历史里名字还在（决策 14）
    set_name: str | None = None
    total_cases: int | None = None
    done_cases: int = 0
    #: 整轮的失败原因（如「这个评测集没有参与评测的用例」）
    error: str | None = None
    #: `finished_at - started_at`，**现算不存列**（少一个要同步的字段）
    duration_ms: int | None = None


class EvalRunList(BaseModel):
    items: list[EvalRunSummary] = []
    total: int = 0
    page: int = 1
    page_size: int = 20


# ---- 报告（§7）-------------------------------------------------------

class EvalReportMetric(BaseModel):
    key: str
    #: 缺项时是 `None` → 前端显示「—」，**不是 0**（决策 13）
    score: float | None = None
    threshold: float
    passed: bool = False


class EvalReportCase(BaseModel):
    case_id: str | None = None
    question: str | None = None
    ground_truth: str | None = None
    answer: str = ""
    context_recall: float | None = None
    context_precision: float | None = None
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    #: 该题四项的均值；缺任一项就是 `None`（不用剩下的三项凑）
    score: float | None = None
    #: **只有真跑挂了才填**，拒答不算失败（决策 20）
    error: str | None = None


class EvalReport(BaseModel):
    composite_score: float | None = None
    metrics: list[EvalReportMetric] = []
    verdict: str = ""
    #: ⚠️ 这两个**必须在 report 里**：报告页四张卡全「—」时，前端得能说清为什么
    #: （环境不在 / 有指标没算出来 / 没有可评分样本）。没有它们就是哑谜。
    ragas_available: bool | None = None
    ragas_errors: list[str] = []


class EvalRunDetail(EvalRunSummary):
    report: EvalReport | None = None
    cases: list[EvalReportCase] = []


# ---- 评测集与用例（§5.1 / §5.2）--------------------------------------

class EvalSetItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    #: 该集**全部**用例数（不是只数 in_eval 的）—— 与界面上的行数对得上，
    #: 用户才不会以为丢了数据（§11.3-4）
    case_count: int = 0
    in_eval_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalSetList(BaseModel):
    items: list[EvalSetItem] = []


class EvalSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class EvalSetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class EvalCaseItem(BaseModel):
    id: str
    question: str
    ground_truth: str | None = None
    case_type: str
    suite: str = "full"
    source: str = "manual"
    in_eval: bool = True
    note: str | None = None
    expected_doc_ids: list[str] | None = None
    source_document_id: str | None = None
    source_chunk_id: str | None = None
    source_page: int | None = None
    #: ⚠️ 列表里**不回** `source_snippet` 正文：一段几百字 × 每页 20 条太占带宽，
    #: 「核对标准答案」弹窗按需走 `GET /cases/{id}/source` 单取。
    created_at: datetime | None = None


class EvalCasePage(BaseModel):
    items: list[EvalCaseItem] = []
    total: int = 0
    page: int = 1
    page_size: int = 20


class EvalCasePatch(BaseModel):
    """可改的列（§5.2）。

    ⚠️ **没有** `source` / `set_id` / `source_*` —— 改了就没有「来源」可言了。
    """
    question: str | None = Field(default=None, min_length=1)
    ground_truth: str | None = None
    in_eval: bool | None = None
    note: str | None = None


class EvalCaseSource(BaseModel):
    question: str
    ground_truth: str | None = None
    source_document_id: str | None = None
    source_document_title: str | None = None
    source_chunk_id: str | None = None
    source_page: int | None = None
    source_snippet: str | None = None
    #: 标准答案在 `source_snippet` 里的字符区间 `[start, end)`；对不上是 `null`
    #: —— 那往往是**文档更新过了**（§8.3）
    highlight: list[int] | None = None


class EvalCaseCreate(BaseModel):
    question: str = Field(min_length=1)
    ground_truth: str | None = None
    in_eval: bool = True
    note: str | None = None


class EvalGenerateRequest(BaseModel):
    document_id: str
    #: 条数**是目标不是保证**（决策 18）；服务端夹到 1..10
    count: int = 5


class EvalGenerateCaseOut(BaseModel):
    id: str
    question: str
    ground_truth: str | None = None


class EvalGenerateResponse(BaseModel):
    requested: int
    created: int
    failed: int
    #: 给前端直接显示的说明（超时 / 没通过校验 / 文档没片段）
    reason: str = ""
    timeout: bool = False
    cases: list[EvalGenerateCaseOut] = []


# ---- 消融对比（§4.3.1.4，原样不动）----------------------------------

class EvalCompareRow(BaseModel):
    run_id: str
    config_label: str
    config: dict[str, Any] = {}
    status: str
    values: dict[str, Any] = {}
    #: 这轮 ragas 跑没跑成、错在哪 —— 是诊断元数据不是指标，故**不进 `values`**
    #: （那会让前端按 key 猜），也不当矩阵列（前者恒 true、后者是列表）。
    #: 前端据此在表下给提示（§4.3.1.4）
    ragas_available: bool | None = None
    ragas_errors: list[str] = []


class EvalCompareResponse(BaseModel):
    #: 指标全集（固定顺序，表头不随数据乱跳）
    metrics: list[str] = []
    runs: list[EvalCompareRow] = []
