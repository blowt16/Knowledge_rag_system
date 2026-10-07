"""评测接口（§3.7.3 / §4.3.1.4 / §5）—— 全部 admin。

⚠️ `POST /run` 是**异步**的：立即返回 `run_id`，**复用 `eval_runs.status` 当任务表**
   （不新建任务表、也不用 SSE —— 轮询足够，§3.7.3 明写评测不做进度流）。
   前端照 `GET /runs/{id}` 轮询到 `done` 即可。

## ⚠️ `POST /run` 的两种模式（§5.4）—— 本文件最容易搞错的一处

`config` 决定这轮是**线上链路**还是**消融运行**，判据是
`eval/config.py` 的 `switches()` —— **config 字典为空 → None → 走线上默认**。

| 来源 | 请求体 | 落到 `eval_runs.config` | 实际跑什么 |
|---|---|---|---|
| **批量评测页**（新） | `{set_id, role}` | `{}` | **线上完整链路** |
| 消融对比页 | 不带 config 或带开关 | `{"suite": "full"}` 或 `{"bm25": true, ...}` | 纯向量基线 / 带开关的消融 |

**只有走 `suite` 老路径时才塞 `suite`。** 走 `set_id` 时 config 保持 `{}` ——
否则新页面的 run 又会被判成消融运行、又跑成纯向量基线（§1.1① 那个坑）。

`set_id` **只写 `eval_runs.set_id` 列，绝不进 `config`**，同一个道理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app import db
from app.core.deps import require_role
from app.core.exceptions import Conflict, NotFound
from app.eval import config as eval_config
from app.eval import generate, report, runner, sets
from app.schemas.eval import (
    EvalCaseCreate,
    EvalCaseItem,
    EvalCasePage,
    EvalCasePatch,
    EvalCaseSource,
    EvalCompareResponse,
    EvalGenerateRequest,
    EvalGenerateResponse,
    EvalRunCreated,
    EvalRunDetail,
    EvalRunList,
    EvalRunRequest,
    EvalSetCreate,
    EvalSetItem,
    EvalSetList,
    EvalSetPatch,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/eval", tags=["eval"])

#: 与 stats/admin/users 同写法（各 api 模块自己声明，不共用全局单例）
AdminUser = Depends(require_role("admin"))

#: 握住后台任务的引用 —— 不然可能被 GC 掉，表现为「run 永远停在 pending」
_TASKS: set[asyncio.Task] = set()


def _items(rows: list[dict], model):
    return [model(**row) for row in rows]


# ============================================================
# 评测集（§5.1）
# ============================================================

@router.get("/sets", response_model=EvalSetList)
async def list_sets(user=AdminUser) -> EvalSetList:
    async with db.tx() as conn:
        rows = await sets.list_sets(conn)
    return EvalSetList(items=_items(rows, EvalSetItem))


@router.post("/sets", response_model=EvalSetItem, status_code=201)
async def create_set(payload: EvalSetCreate, user=AdminUser) -> EvalSetItem:
    async with db.tx() as conn:
        try:
            row = await sets.create_set(conn, payload.name, payload.description,
                                        created_by=user.id)
        except sets.SetNameTaken:
            raise Conflict("已有同名评测集") from None
    # 新建的集一条用例都没有，这两个计数列直接补 0
    return EvalSetItem(**{**row, "case_count": 0, "in_eval_count": 0})


@router.patch("/sets/{set_id}", response_model=EvalSetItem)
async def patch_set(set_id: str, payload: EvalSetPatch, user=AdminUser) -> EvalSetItem:
    async with db.tx() as conn:
        try:
            await sets.update_set(conn, set_id, name=payload.name,
                                  description=payload.description)
        except sets.SetNotFound:
            raise NotFound("评测集不存在") from None
        except sets.SetNameTaken:
            raise Conflict("已有同名评测集") from None
        rows = await sets.list_sets(conn)
    row = next((s for s in rows if s["id"] == set_id), None)
    if row is None:                     # 并发下被删掉了
        raise NotFound("评测集不存在")
    return EvalSetItem(**row)


@router.delete("/sets/{set_id}", status_code=204)
async def delete_set(set_id: str, user=AdminUser) -> Response:
    """删除（连带它的用例；历史 run 不受影响 —— 它存的是 `set_name` 快照）。"""
    async with db.tx() as conn:
        try:
            await sets.delete_set(conn, set_id)
        except sets.SetNotFound:
            raise NotFound("评测集不存在") from None
        except sets.SetBusy as e:
            # §5.5 的守卫：跑到一半删了它，落库时会外键报错 → 整轮 failed
            raise Conflict(str(e)) from None
    return Response(status_code=204)


@router.get("/sets/{set_id}/export")
async def export_set(set_id: str, user=AdminUser) -> Response:
    """导出该评测集为 json 文件（决策 21）。

    ⚠️ **界面不能直接 `<a href="/api/...">`** —— 那条路带不上 `Authorization` 头。
       前端走 `requestBlob` 取回文件再触发下载（见 `lib/download.ts`）。

    ⚠️ **文件名要消毒**：评测集名里可能有 `/ \\ : * ? " < > |` 这些在 Windows 上非法、
       或会让 `Content-Disposition` 头断行的字符。用 `filename*=UTF-8''`（RFC 5987）
       保证中文名不乱码。
    """
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT name FROM eval_sets WHERE id = $1", set_id)
        if row is None:
            raise NotFound("评测集不存在")
        payload = await sets.export_payload(conn, row["name"])

    filename = quote(f"{sets.safe_filename(row['name'])}.json", safe="")
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ============================================================
# 用例（§5.2）
# ============================================================

@router.get("/sets/{set_id}/cases", response_model=EvalCasePage)
async def list_cases(set_id: str,
                     source: str | None = Query(None, pattern="^(manual|generated)$"),
                     q: str | None = None,
                     page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100),
                     user=AdminUser) -> EvalCasePage:
    """筛选与搜索**走后端**，不前端过滤 —— 分页之下前端过滤必然是错的。"""
    async with db.tx() as conn:
        if not await conn.fetchval("SELECT 1 FROM eval_sets WHERE id = $1", set_id):
            raise NotFound("评测集不存在")
        data = await sets.list_cases(conn, set_id, source=source, q=q,
                                     page=page, page_size=page_size)
    return EvalCasePage(items=_items(data["items"], EvalCaseItem),
                        total=data["total"], page=data["page"],
                        page_size=data["page_size"])


@router.post("/sets/{set_id}/cases", response_model=EvalCaseItem, status_code=201)
async def create_case(set_id: str, payload: EvalCaseCreate,
                      user=AdminUser) -> EvalCaseItem:
    async with db.tx() as conn:
        try:
            row = await sets.create_case(conn, set_id, question=payload.question,
                                         ground_truth=payload.ground_truth,
                                         in_eval=payload.in_eval, note=payload.note)
        except sets.SetNotFound:
            raise NotFound("评测集不存在") from None
    return EvalCaseItem(**row)


@router.post("/sets/{set_id}/generate", response_model=EvalGenerateResponse)
async def generate_cases(set_id: str, payload: EvalGenerateRequest,
                         user=AdminUser) -> EvalGenerateResponse:
    """从文档自动生成用例（§8）。

    **同步等待**（决策 18）：一轮最多 10 条、并发 3 路、总超时 120 秒。
    超时返回**部分结果**，已经生成的留在库里 —— 不因为超时把库里的删掉。
    """
    try:
        stats = await generate.generate_cases(set_id, payload.document_id, payload.count)
    except sets.SetNotFound:
        raise NotFound("评测集或文档不存在") from None
    return EvalGenerateResponse(**stats)


@router.get("/cases/{case_id}/source", response_model=EvalCaseSource)
async def case_source(case_id: str, user=AdminUser) -> EvalCaseSource:
    """「核对标准答案」弹窗吃的数据（§5.2 / §6.5）。"""
    async with db.tx() as conn:
        try:
            payload = await sets.case_source(conn, case_id)
        except sets.CaseNotFound:
            raise NotFound("用例不存在") from None
    return EvalCaseSource(**payload)


@router.patch("/cases/{case_id}", response_model=EvalCaseItem)
async def patch_case(case_id: str, payload: EvalCasePatch,
                     user=AdminUser) -> EvalCaseItem:
    """可改的列只有 `question` / `ground_truth` / `in_eval` / `note`（§5.2）。

    `source` / `set_id` / `source_*` 改了就没有「来源」可言了，schema 里就没有它们。
    """
    async with db.tx() as conn:
        try:
            row = await sets.update_case(conn, case_id,
                                         **payload.model_dump(exclude_unset=True))
        except sets.CaseNotFound:
            raise NotFound("用例不存在") from None
    return EvalCaseItem(**row)


@router.delete("/cases/{case_id}", status_code=204)
async def delete_case(case_id: str, user=AdminUser) -> Response:
    """删用例。**历史结果保留**，只是那条结果的 `case_id` 置空（外键 SET NULL）。"""
    async with db.tx() as conn:
        try:
            await sets.delete_case(conn, case_id)
        except sets.CaseNotFound:
            raise NotFound("用例不存在") from None
        except sets.SetBusy as e:
            raise Conflict(str(e)) from None
    return Response(status_code=204)


# ============================================================
# 运行（§5.3 / §5.4）
# ============================================================

@router.post("/run", response_model=EvalRunCreated, status_code=202)
async def start_run(payload: EvalRunRequest, user=AdminUser) -> EvalRunCreated:
    """起一轮评测。立即返回 run_id（202），跑完看 `GET /runs/{id}`。"""
    run_id = uuid.uuid4().hex

    if not payload.set_id and payload.suite not in ("full", "refusal_calib"):
        raise HTTPException(400, detail="suite 只能是 full / refusal_calib")

    async with db.tx() as conn:
        # 一次只允许一轮在跑：评测要占 GPU，两轮并跑既慢又会把显存挤爆
        running = await conn.fetchval(
            "SELECT count(*) FROM eval_runs WHERE status IN ('pending','running')")
        if running:
            raise Conflict("已有评测在跑，等它结束再起")

        set_name = None
        if payload.set_id:
            row = await conn.fetchrow("SELECT name FROM eval_sets WHERE id = $1",
                                      payload.set_id)
            if row is None:
                raise NotFound("评测集不存在")
            # 快照：评测集删了，历史里名字还在（决策 14）
            set_name = row["name"]

        # ⚠️ 两种模式的**唯一分界**（§5.4）：走 `set_id` 时 config 保持 `{}` ——
        #    非空就会被判成消融运行、又跑成纯向量基线，而界面写着「线上链路」。
        #    ⚠️ `set_id` **绝不进 config 字典**，它只写下面那一列。
        cfg: dict = {}
        if not payload.set_id:
            cfg = dict(payload.config or {})
            cfg["suite"] = payload.suite
            if payload.case_ids:
                cfg["case_ids"] = payload.case_ids

        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted,
                                      status, set_id, set_name)
               VALUES ($1,$2,$3,$4,$5,'pending',$6,$7)""",
            run_id, payload.name, cfg,
            payload.role, 1 if payload.include_restricted else 0,
            payload.set_id, set_name)

    task = asyncio.create_task(runner.run_eval(run_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    # ⚠️ label 读的是**落库的 `cfg`**，不是请求体里的 `payload.config`。
    #    读请求体的话，消融页点「跑全量」（不带 config）会显示「线上链路」，
    #    而这一轮实际跑的是纯向量基线、列表里也写着「纯向量检索」——
    #    **创建响应的那一刻就在骗人**，方向还正好和 §1.1① 相反。
    return EvalRunCreated(run_id=run_id, status="pending",
                          config_label=eval_config.label(cfg))


@router.get("/runs", response_model=EvalRunList)
async def list_runs(page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100),
                    user=AdminUser) -> EvalRunList:
    """任务表（§6.3）。分页在服务端做。"""
    offset = (page - 1) * page_size
    async with db.tx() as conn:
        total = await conn.fetchval("SELECT count(*) FROM eval_runs")
        rows = await conn.fetch(
            """SELECT id, name, config, role, include_restricted, status, metrics,
                      started_at, finished_at, set_name, total_cases, done_cases, error
                 FROM eval_runs
                ORDER BY COALESCE(started_at, now()) DESC, id
                LIMIT $1 OFFSET $2""", page_size, offset)

    items = []
    for r in rows:
        cfg = _as_dict(r["config"]) or {}
        items.append({
            "run_id": r["id"], "name": r["name"],
            "config": cfg, "config_label": eval_config.label(cfg),
            "role": r["role"], "status": r["status"],
            "include_restricted": bool(r["include_restricted"]),
            "metrics": _as_dict(r["metrics"]),
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "set_name": r["set_name"], "total_cases": r["total_cases"],
            "done_cases": r["done_cases"] or 0, "error": r["error"],
            "duration_ms": report.duration_ms(dict(r)),
        })
    return EvalRunList(items=items, total=total, page=page, page_size=page_size)


@router.get("/runs/{run_id}", response_model=EvalRunDetail)
async def get_run(run_id: str, user=AdminUser) -> EvalRunDetail:
    """`GET /runs/{id}` —— 「看报告」弹窗吃这个（§7.1）。

    ⚠️ 报告口径**在服务端算**（`eval/report.py`）：指标会演进，前端拼公式
       就会出现「同一份数据算出两个分」。
    """
    async with db.tx() as conn:
        run = await conn.fetchrow("SELECT * FROM eval_runs WHERE id=$1", run_id)
        if run is None:
            raise NotFound("评测不存在")
        results = await conn.fetch(
            """SELECT case_id, question, ground_truth, answer, error, metrics
                 FROM eval_case_results WHERE run_id=$1 ORDER BY created_at, id""",
            run_id)

    cfg = _as_dict(run["config"]) or {}
    built = report.build(dict(run), [dict(r) for r in results])
    return EvalRunDetail(
        run_id=run["id"], name=run["name"], config=cfg,
        config_label=eval_config.label(cfg), role=run["role"], status=run["status"],
        include_restricted=bool(run["include_restricted"]),
        metrics=_as_dict(run["metrics"]),
        started_at=run["started_at"], finished_at=run["finished_at"],
        set_name=run["set_name"], total_cases=run["total_cases"],
        done_cases=run["done_cases"] or 0, error=run["error"],
        duration_ms=report.duration_ms(dict(run)),
        report=built["report"], cases=built["cases"],
    )


@router.delete("/runs/{id}", status_code=204)
async def delete_run(id: str, user=AdminUser) -> Response:
    """删一轮 = 连它的逐题结果一起删（靠外键 ON DELETE CASCADE）。

    **删掉的 run 不再出现在 `/compare` 里** —— 这是有意的，删就是删。

    ⚠️ **正在跑的轮次不许删**：删行**不会停掉后台那条任务**，它还在占 GPU。
       行没了之后「已有评测在跑」的检查就查不到任何东西，用户可以立刻再起一轮 ——
       两轮同时跑，正是上面写的「两轮并跑既慢又会把显存挤爆」；
       而上一轮最后会静默失败（更新 0 行、批量 INSERT 外键违约），用户什么都看不到。
       真要中止得先有 abort 语义，本轮不做（见方案 §5.3）。
    """
    async with db.tx() as conn:
        status = await conn.fetchval("SELECT status FROM eval_runs WHERE id = $1", id)
        if status is None:
            raise NotFound("评测不存在")
        if status in ("pending", "running"):
            raise Conflict("这一轮还在跑，删掉它不会真的停下（还在占 GPU）—— 等它结束再删")
        await conn.execute("DELETE FROM eval_runs WHERE id = $1", id)
    return Response(status_code=204)


@router.get("/compare", response_model=EvalCompareResponse)
async def compare(run_ids: str = Query(..., description="逗号分隔的 run_id"),
                  user=AdminUser) -> EvalCompareResponse:
    """消融对比表（§4.3.1.4）—— **服务端**产出配置 + 对齐后的指标矩阵。

    ⚠️ 不由前端拼：指标全集由服务端掌握，不同 run 可能缺指标、指标名会演进，
       前端拼会把这些逻辑复制一份到前端，且 `config_label` 会各拼各的。

    ⚠️ **本接口本轮原样不动**（决策 22）：它读的是 `suite` 路径产出的老 run，
       与新评测集功能互不干扰。
    """
    ids = [x.strip() for x in run_ids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(400, detail="run_ids 不能为空")

    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id, config, metrics, status FROM eval_runs WHERE id = ANY($1::text[])", ids)

    by_id = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(404, detail=f"这些 run 不存在：{missing}")

    # 指标全集：所有 run 出现过的键，**按固定顺序**（不然表头会随数据乱跳）
    preferred = ["recall_at_k", "mrr", "avg_ms", "refusal_rate",
                 "unauthorized_hits", "uncited_ratio", "citation_invalid",
                 "cases", "failed", "answered_count", "knowledge_count"]
    ragas_keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    # ⚠️ 这三个不是指标，**不能当矩阵列**：`ragas` 是嵌套子字典（下面拍平进 values），
    #    `ragas_available` 恒为 true、`ragas_errors` 是列表 —— 当列只是噪声。
    #    它们走后两个（`ragas_*` 作为行上的诊断字段，见 EvalCompareRow）。
    non_metric_keys = {"ragas", "ragas_available", "ragas_errors"}
    seen: set[str] = set()
    for r in rows:
        seen |= set((_as_dict(r["metrics"]) or {}).keys())
    metrics = ([k for k in preferred if k in seen]
               + sorted(k for k in seen
                        if k not in preferred and k not in non_metric_keys))

    runs = []
    for i in ids:                      # 保持传入顺序 —— 叠加表的行序有含义
        r = by_id[i]
        cfg = _as_dict(r["config"]) or {}
        m = _as_dict(r["metrics"]) or {}
        values = {k: m.get(k) for k in metrics}
        ragas = m.get("ragas") or {}
        values.update({k: ragas.get(k) for k in ragas_keys})
        runs.append({"run_id": i, "config_label": eval_config.label(cfg),
                     "config": cfg, "status": r["status"], "values": values,
                     "ragas_available": m.get("ragas_available"),
                     "ragas_errors": m.get("ragas_errors") or []})

    # ragas 四项排最前：指标全集十几个，一屏放不下必定横向滚动，
    # 排末尾等于默认看不见（§4.3.1.4 的消融表就该先看这几列）
    return EvalCompareResponse(metrics=ragas_keys + metrics, runs=runs)


def _as_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value
