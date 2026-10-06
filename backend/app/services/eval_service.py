"""评测链路（§3.7.3 / §5.2 / §10.2 M5-2）。

跑一轮评测 = **把题库里的每一题喂进真实图**，收集答案与检索结果，算指标，落库。

⚠️ **走真实图，不在评测器里重搭一条链路**（M2 已定，理由照旧）：
   重搭的那份迟早和 `builder.py` 漂移，漂移之后指标照样好看、只是不再反映线上行为。

⚠️ **评测不写 `qa_logs`**：结果只落 `eval_case_results`。这样
   `stats/overview.qa_count` 天然不含评测轮次，不需要额外过滤。
   哪天要让评测也写日志，**必须同时给 qa_count 加过滤**（M5 开工口径第 2 条）。

⚠️ **与线上问答共用同一个 GPU 信号量**（§10.2 M5-2）：评测走的是
   `reranker.rerank()`，它内部用 `reranker._get_semaphore()` —— 与线上同一把锁，
   **不做优先级抢占**（评测是后台任务，慢一点没关系，抢显存会连线上一起搞崩）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from app import db
from app.graph.builder import get_graph
from app.graph.state import UserContextLite, new_state
from app.services import ragas_service

logger = logging.getLogger(__name__)

#: 检索指标看前几名
TOP_K = 5


async def _drive_to_end(state: dict) -> dict:
    """把图跑到 END，返回最终状态。

    ⚠️ 与 M2 的 `_drive_to_retrieve` 不同 —— 那个**特意停在 generate 之前**
       （M2 只考查询理解，跑到底会把 M3 生成侧的缺陷混进指标）。M5 要算
       ragas 的忠实度与答案相关性，**必须有答案**，所以这次要跑到底。
    """
    final: dict = {}
    async for mode, payload in get_graph().astream(state, stream_mode=["values"]):
        if mode == "values":
            final = payload or {}
    return final


async def _load_visibility(conn) -> dict[str, set[str]]:
    """文档可见性：doc_id -> 能看见它的角色集合（public 视为三角色都可见）。"""
    rows = await conn.fetch(
        "SELECT id, visibility, visible_roles FROM documents WHERE status='active'")
    out: dict[str, set[str]] = {}
    for r in rows:
        roles: set[str] = set()
        if r["visibility"] == "public":
            roles = {"student", "staff", "admin"}
        else:
            raw = r["visible_roles"]
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or [])
            roles = set(parsed or [])
        out[r["id"]] = roles
    return out


def _rank_chunks(final: dict) -> list:
    """最终顺序的候选（与线上 build_context 的输入同源）。"""
    return final.get("reranked") or final.get("candidates") or []


def _case_metrics(final: dict, case: dict, visibility: dict[str, set[str]],
                  role: str) -> dict:
    """单题的**自定义**指标（ragas 四项另外算）。"""
    chunks = _rank_chunks(final)[:TOP_K]
    doc_ids = [c.document_id for c in chunks]

    expected = case.get("expected_doc_ids")
    expected_ids = set(expected or []) if not isinstance(expected, str) else set(
        json.loads(expected) or [])

    first_hit = next((i for i, d in enumerate(doc_ids, start=1) if d in expected_ids), 0)

    # 越权：返回的 chunk 里属于「该角色看不见的文档」的条数（§5.3 读它）
    unauthorized = sum(
        1 for d in doc_ids
        if d and role not in (visibility.get(d) or {"student", "staff", "admin"}))

    report = final.get("verify_report") or {}
    if hasattr(report, "model_dump"):
        report = report.model_dump()
    total_claims = int(report.get("total_claims") or 0)
    uncited = len(report.get("uncited_claims") or [])
    invalid = len(report.get("invalid_markers") or [])

    return {
        "recall_at_k": 1.0 if first_hit else 0.0,
        "mrr": (1.0 / first_hit) if first_hit else 0.0,
        "rank": first_hit,
        "refused": bool(final.get("refused")),
        "refusal_reason": final.get("refusal_reason") or "",
        "route": final.get("route") or "",
        "decision": final.get("decision") or "",
        "unauthorized_hits": unauthorized,
        "citation_invalid": invalid,
        "citation_total": int(report.get("cited_claims") or 0),
        "uncited_claims": uncited,
        "total_claims": total_claims,
        "uncited_ratio": (uncited / total_claims) if total_claims else None,
    }


async def run_eval(run_id: str) -> None:
    """跑一轮评测。**后台任务** —— 立即返回 run_id，进度看 `eval_runs.status`。"""
    started = time.time()
    try:
        async with db.tx() as conn:
            run = await conn.fetchrow("SELECT * FROM eval_runs WHERE id = $1", run_id)
            if run is None:
                logger.error("评测 run 不存在", extra={"event": "eval.missing_run", "run_id": run_id})
                return
            await conn.execute(
                "UPDATE eval_runs SET status='running', started_at=now() WHERE id=$1", run_id)

        role = run["role"] or "student"
        include_restricted = bool(run["include_restricted"])
        config = run["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)
        suite = (config.get("suite") or "full") if isinstance(config, dict) else "full"
        case_ids = (config.get("case_ids") if isinstance(config, dict) else None) or []

        case_type = config.get("case_type") if isinstance(config, dict) else None

        async with db.tx() as conn:
            if case_ids:
                rows = await conn.fetch(
                    "SELECT * FROM eval_cases WHERE id = ANY($1::text[]) ORDER BY id", case_ids)
            elif case_type:
                # ACL 对照实验用：只跑某一类题（如 case_type='restricted'）
                rows = await conn.fetch(
                    "SELECT * FROM eval_cases WHERE case_type=$1 ORDER BY id", case_type)
            elif suite == "refusal_calib":
                rows = await conn.fetch(
                    "SELECT * FROM eval_cases WHERE suite='refusal_calib' ORDER BY id")
            else:
                rows = await conn.fetch("SELECT * FROM eval_cases ORDER BY id")
            visibility = await _load_visibility(conn)

        if not rows:
            async with db.tx() as conn:
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), "
                    "metrics=$2 WHERE id=$1",
                    run_id, {"error": "no_cases"})
            logger.warning("评测没有可跑的用例", extra={"event": "eval.no_cases"})
            return

        results: list[dict] = []
        ragas_samples: list[dict] = []
        ragas_index: list[int] = []

        for case in rows:
            case = dict(case)
            state = new_state(
                query=case["question"],
                session_id=f"eval-{run_id[:8]}",
                user=UserContextLite(id="eval", role=role),
                history=[],
                include_restricted=include_restricted,
                eval_config=config if isinstance(config, dict) else {},
            )
            t0 = time.perf_counter()
            try:
                final = await _drive_to_end(state)
            except Exception as e:  # noqa: BLE001 —— 单题失败不该拖垮整轮
                logger.warning("评测用例执行失败：%s", e,
                               extra={"event": "eval.case_failed", "case_id": case["id"]})
                results.append({"case_id": case["id"], "error": f"{type(e).__name__}: {e}"})
                continue
            elapsed = int((time.perf_counter() - t0) * 1000)

            metrics = _case_metrics(final, case, visibility, role)
            metrics["ms"] = elapsed

            answer = final.get("answer") or ""
            contexts = [c.text for c in _rank_chunks(final)[:TOP_K]]
            results.append({
                "case_id": case["id"], "metrics": metrics, "answer": answer,
                "retrieved_ids": [c.chunk_id for c in _rank_chunks(final)[:TOP_K]],
                "unauthorized_hits": metrics["unauthorized_hits"],
            })

            # 有答案、有标准答案才送 ragas（拒答题没有 ground_truth）
            if answer and case.get("ground_truth") and contexts:
                ragas_samples.append({
                    "user_input": case["question"], "response": answer,
                    "retrieved_contexts": contexts,
                    "reference": case["ground_truth"],
                })
                ragas_index.append(len(results) - 1)

        # ---- ragas 四项（隔离环境子进程，一次整批）----------------------
        ragas = await ragas_service.score_samples(ragas_samples) if ragas_samples else {
            "available": True, "ok": True, "rows": [], "means": {}, "errors": []}
        for pos, row in zip(ragas_index, ragas.get("rows") or []):
            results[pos]["metrics"].update({f"ragas_{k}": v for k, v in row.items()})

        # ---- 落库 ------------------------------------------------------
        async with db.tx() as conn:
            for r in results:
                await conn.execute(
                    """INSERT INTO eval_case_results
                         (id, run_id, case_id, retrieved_ids, answer, unauthorized_hits, metrics)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    uuid.uuid4().hex, run_id, r["case_id"],
                    r.get("retrieved_ids") or [],
                    r.get("answer") or "",
                    int(r.get("unauthorized_hits") or 0),
                    r.get("metrics") or {},
                )

            summary = _aggregate(results, ragas)
            await conn.execute(
                "UPDATE eval_runs SET status='done', finished_at=now(), metrics=$2 WHERE id=$1",
                run_id, summary)

        logger.info("评测完成", extra={"event": "eval.done", "run_id": run_id,
                                    "cases": len(results),
                                    "ms": int((time.time() - started) * 1000)})
    except Exception as e:  # noqa: BLE001
        logger.exception("评测整轮失败", extra={"event": "eval.failed", "run_id": run_id})
        try:
            async with db.tx() as conn:
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), metrics=$2 "
                    "WHERE id=$1", run_id,
                    {"error": f"{type(e).__name__}: {e}"})
        except Exception:  # noqa: BLE001
            pass


def _aggregate(results: list[dict], ragas: dict) -> dict:
    """整轮汇总：自定义指标求均值 + ragas 四指标取均值。"""
    ok = [r for r in results if "metrics" in r]
    n = len(ok) or 1

    def _avg(key: str):
        vals = [r["metrics"][key] for r in ok
                if isinstance(r["metrics"].get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else None

    answered = [r for r in ok if not r["metrics"].get("refused")]
    with_gt = [r for r in ok if r["metrics"].get("rank") is not None
               and r["metrics"].get("rank", 0) >= 0]

    summary: dict[str, Any] = {
        "cases": len(results),
        "failed": len(results) - len(ok),
        "recall_at_k": _avg("recall_at_k"),
        "mrr": _avg("mrr"),
        "avg_ms": _avg("ms"),
        "refused_count": sum(1 for r in ok if r["metrics"].get("refused")),
        "unauthorized_hits": sum(int(r["metrics"].get("unauthorized_hits") or 0) for r in ok),
        "uncited_ratio": _avg("uncited_ratio"),
        "citation_invalid": sum(int(r["metrics"].get("citation_invalid") or 0) for r in ok),
        # ragas 四指标（跑不起来时为 None，不假装有数）
        "ragas": ragas.get("means") or {},
        "ragas_available": bool(ragas.get("available")),
        "ragas_errors": ragas.get("errors") or [],
    }
    # 拒答率的分母按 §9.0 的 M4-D2 口径：**只数 route='knowledge' 的轮次**
    knowledge = [r for r in ok if r["metrics"].get("route") == "knowledge"]
    summary["knowledge_count"] = len(knowledge)
    summary["refusal_rate"] = (
        round(sum(1 for r in knowledge if r["metrics"].get("refused")) / len(knowledge), 4)
        if knowledge else None)
    summary["answered_count"] = len(answered)
    summary["scored_with_ground_truth"] = len(with_gt)
    return summary
