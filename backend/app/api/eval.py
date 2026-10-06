"""评测接口（§3.7.3 / §4.3.1.4）—— 全部 admin。

⚠️ `POST /run` 是**异步**的：立即返回 `run_id`，**复用 `eval_runs.status` 当任务表**
   （不新建任务表、也不用 SSE —— 轮询足够，§3.7.3 明写评测不做进度流）。
   前端照 `GET /runs/{id}` 轮询到 `done` 即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app import db
from app.core.deps import require_role
from app.retrieval import eval_config
from app.schemas.eval import (
    EvalCompareResponse,
    EvalRunCreated,
    EvalRunDetail,
    EvalRunList,
    EvalRunRequest,
)
from app.services import eval_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/eval", tags=["eval"])

#: 与 stats/admin/users 同写法（各 api 模块自己声明，不共用全局单例）
AdminUser = Depends(require_role("admin"))

#: 握住后台任务的引用 —— 不然可能被 GC 掉，表现为「run 永远停在 pending」
_TASKS: set[asyncio.Task] = set()


@router.post("/run", response_model=EvalRunCreated, status_code=202)
async def start_run(payload: EvalRunRequest, user=AdminUser) -> EvalRunCreated:
    """起一轮评测。立即返回 run_id（202），跑完看 `GET /runs/{id}`。"""
    run_id = uuid.uuid4().hex

    if payload.suite not in ("full", "refusal_calib"):
        raise HTTPException(400, detail="suite 只能是 full / refusal_calib")
    # 一次只允许一轮在跑：评测要占 GPU，两轮并跑既慢又会把显存挤爆
    async with db.tx() as conn:
        running = await conn.fetchval(
            "SELECT count(*) FROM eval_runs WHERE status IN ('pending','running')")
        if running:
            raise HTTPException(409, detail="已有评测在跑，等它结束再起")

        cfg = dict(payload.config or {})
        cfg["suite"] = payload.suite
        if payload.case_ids:
            cfg["case_ids"] = payload.case_ids
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted, status)
               VALUES ($1,$2,$3,$4,$5,'pending')""",
            run_id, payload.name, cfg,
            payload.role, 1 if payload.include_restricted else 0)

    task = asyncio.create_task(eval_service.run_eval(run_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    return EvalRunCreated(run_id=run_id, status="pending",
                          config_label=eval_config.label(payload.config))


@router.get("/runs", response_model=EvalRunList)
async def list_runs(limit: int = Query(20, ge=1, le=100), user=AdminUser) -> EvalRunList:
    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id, name, config, role, include_restricted, status, metrics, "
            "       started_at, finished_at FROM eval_runs "
            "ORDER BY COALESCE(started_at, now()) DESC LIMIT $1", limit)
    items = []
    for r in rows:
        cfg = r["config"] or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        items.append({
            "run_id": r["id"], "name": r["name"],
            "config": cfg, "config_label": eval_config.label(cfg),
            "role": r["role"], "status": r["status"],
            "include_restricted": bool(r["include_restricted"]),
            "metrics": _as_dict(r["metrics"]),
            "started_at": r["started_at"], "finished_at": r["finished_at"],
        })
    return EvalRunList(items=items)


@router.get("/runs/{run_id}", response_model=EvalRunDetail)
async def get_run(run_id: str, user=AdminUser) -> EvalRunDetail:
    async with db.tx() as conn:
        run = await conn.fetchrow("SELECT * FROM eval_runs WHERE id=$1", run_id)
        if run is None:
            raise HTTPException(404, detail="评测不存在")
        results = await conn.fetch(
            "SELECT case_id, retrieved_ids, unauthorized_hits, metrics, answer "
            "FROM eval_case_results WHERE run_id=$1 ORDER BY case_id", run_id)
    cfg = run["config"] or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    return EvalRunDetail(
        run_id=run["id"], name=run["name"], config=cfg,
        config_label=eval_config.label(cfg), role=run["role"], status=run["status"],
        include_restricted=bool(run["include_restricted"]),
        metrics=_as_dict(run["metrics"]),
        started_at=run["started_at"], finished_at=run["finished_at"],
        cases=[{
            "case_id": r["case_id"],
            "retrieved_ids": _as_dict(r["retrieved_ids"]) or [],
            "unauthorized_hits": r["unauthorized_hits"],
            "metrics": _as_dict(r["metrics"]) or {},
        } for r in results],
    )


@router.get("/compare", response_model=EvalCompareResponse)
async def compare(run_ids: str = Query(..., description="逗号分隔的 run_id"),
                  user=AdminUser) -> EvalCompareResponse:
    """消融对比表（§4.3.1.4）—— **服务端**产出配置 + 对齐后的指标矩阵。

    ⚠️ 不由前端拼：指标全集由服务端掌握，不同 run 可能缺指标、指标名会演进，
       前端拼会把这些逻辑复制一份到前端，且 `config_label` 会各拼各的。
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
        cfg = r["config"] or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
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
