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
from app.eval import ragas as ragas_service

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

    # ⚠️ **没有 `expected_doc_ids` 的题，排名类指标一律记 `None`，不是 0**（§7.6）。
    #    界面上手工录入的用例都没有期望文档（弹窗里没这一项）。记 0 的话，
    #    `_avg()` 照样把它算进均值 → **整轮的 Recall@5 / MRR 被这批题凭空拉低**，
    #    而且逐题单看都"正常"，查不出原因。
    #    `_avg()` 现有的 `isinstance(v, (int, float))` 过滤会自动跳过 `None`，
    #    聚合逻辑一行都不用改。
    #    ⚠️ 别和「有期望但没召回」混为一谈 —— 那种**就是 0 分**（下面 else 支）。
    first_hit = (next((i for i, d in enumerate(doc_ids, start=1) if d in expected_ids), 0)
                 if expected_ids else None)

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
        "recall_at_k": None if first_hit is None else (1.0 if first_hit else 0.0),
        "mrr": None if first_hit is None else ((1.0 / first_hit) if first_hit else 0.0),
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


async def _select_cases(conn, *, set_id: str | None, config: dict) -> list:
    """选题优先级（§5.5）。**分支顺序有意义，别调换。**

        set_id 给了        → WHERE set_id = $1 AND in_eval = TRUE     ← 新页面（批量评测）
        case_ids 给了      → WHERE id = ANY($1)                       ← 调试用
        case_type 给了     → WHERE case_type = $1                     ← ACL 对照实验用
        suite 给了         → 保持原 SQL，**不碰集合**                 ← 决策 22
              'full'          → SELECT * FROM eval_cases               （全部 90 条）
              'refusal_calib' → WHERE suite='refusal_calib'            （15 条）
        都不给             → 全部                                     ← 兜底

    ⚠️ **`suite` 路径一行 SQL 都不动**（决策 22）。现在 `suite='full'` 实际跑的是
       **全部 90 条**（含那 15 条校准题）。按 `suite` 分组迁进两个集会让它变成 75 条，
       而差的那 15 条多是**该拒答**的，混进来会拉低召回率 —— 一旦题量变了，
       消融 8 行表的**历史行与新行题集不同**，表里的差值就不再只反映配置差异。

    ⚠️ **`in_eval` 只对 `set_id` 路径生效。** `suite` 路径（CI 的 `eval-calibration`、
       `run_ablation.py`）**不过滤** `in_eval`：那些入口的题集必须稳定 ——
       有人在界面上随手关掉一条校准题，就把 CI 门禁的分母改了，
       甚至可能直接空集导致 CI 变红。这种事不该由一次误点触发。

       **代价如实写明**：同一道题，在批量评测页关掉后不再参与，但在消融/CI 里照样跑。
       所以界面上那个开关的含义更接近「参与批量评测」。
    """
    if set_id:
        return list(await conn.fetch(
            "SELECT * FROM eval_cases WHERE set_id = $1 AND in_eval = TRUE ORDER BY id",
            set_id))
    case_ids = (config or {}).get("case_ids") or []
    if case_ids:
        return list(await conn.fetch(
            "SELECT * FROM eval_cases WHERE id = ANY($1::text[]) ORDER BY id", case_ids))
    case_type = (config or {}).get("case_type")
    if case_type:
        return list(await conn.fetch(
            "SELECT * FROM eval_cases WHERE case_type=$1 ORDER BY id", case_type))
    suite = (config or {}).get("suite") or "full"
    if suite == "refusal_calib":
        return list(await conn.fetch(
            "SELECT * FROM eval_cases WHERE suite='refusal_calib' ORDER BY id"))
    return list(await conn.fetch("SELECT * FROM eval_cases ORDER BY id"))


async def _record_progress(run_id: str, done: int, total: int | None = None) -> None:
    """推进度（§3.4）。

    `done_cases` / `total_cases` 只有一个消费方，但那一个是**必须**的：
    「看报告」按钮在 `pending`/`running` 时置灰，悬停提示要写「评测还在跑（3/5）」。
    没有这两个数，那个提示就只能写「完成后才能看」，用户完全不知道要等多久（§6.3）。
    """
    async with db.tx() as conn:
        if total is None:
            await conn.execute("UPDATE eval_runs SET done_cases = $2 WHERE id = $1",
                               run_id, done)
        else:
            await conn.execute(
                "UPDATE eval_runs SET done_cases = $2, total_cases = $3 WHERE id = $1",
                run_id, done, total)


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
        # ⚠️ 集合归属只在**列**上，`config` 里绝不带 `set_id`（§3.4）：
        #    进了 config 字典就非空 → `switches()` 返回非 None → 又跑成纯向量基线。
        set_id = run["set_id"]
        config = run["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)

        async with db.tx() as conn:
            rows = await _select_cases(conn, set_id=set_id, config=config)
            visibility = await _load_visibility(conn)

        if not rows:
            # 选到空集 → 直接标 failed，**不跑出一个空报告**（§5.5）
            error = ("这个评测集没有参与评测的用例"
                     if set_id else "no_cases")
            async with db.tx() as conn:
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), "
                    "metrics=$2, error=$3, total_cases=0, done_cases=0 WHERE id=$1",
                    run_id, {"error": error}, error)
            logger.warning("评测没有可跑的用例",
                           extra={"event": "eval.no_cases", "set_id": set_id})
            return

        await _record_progress(run_id, 0, len(rows))

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
                # ⚠️ 这里**必须带上 question / ground_truth 快照**：失败的那行不会有
                #    `metrics`，历史报告里的「问题」列就只能靠快照列填（§3.5）。
                results.append({"case_id": case["id"], "error": f"{type(e).__name__}: {e}",
                                "question": case["question"],
                                "ground_truth": case.get("ground_truth")})
                await _record_progress(run_id, len(results))
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
                # 快照（§3.5）：跑的时候把该题的问题与标准答案抄一份进来，
                # 历史报告从此**自给自足**，不再需要 join `eval_cases`
                "question": case["question"],
                "ground_truth": case.get("ground_truth"),
            })
            await _record_progress(run_id, len(results))

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
                         (id, run_id, case_id, retrieved_ids, answer, unauthorized_hits,
                          metrics, question, ground_truth, error)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    uuid.uuid4().hex, run_id, r["case_id"],
                    r.get("retrieved_ids") or [],
                    r.get("answer") or "",
                    int(r.get("unauthorized_hits") or 0),
                    r.get("metrics") or {},
                    # ⚠️ 这三列以前**被丢掉了**：原来只取 `r.get("metrics")`，
                    #    而失败的那条记录根本没有 `metrics` 键 —— 于是库里所有失败题
                    #    的「失败原因」都是空的，界面那一列永远是空白（§1.1③）。
                    r.get("question"), r.get("ground_truth"), r.get("error"),
                )

            summary = _aggregate(results, ragas)
            await conn.execute(
                "UPDATE eval_runs SET status='done', finished_at=now(), metrics=$2, "
                "done_cases=$3 WHERE id=$1",
                run_id, summary, len(results))

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
