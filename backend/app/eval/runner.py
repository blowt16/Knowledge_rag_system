"""评测链路（§3.7.3 / §5.2 / §10.2 M5-2）。

跑一轮评测 = **把题库里的每一题喂进真实图**，收集答案与检索结果，算指标，落库。

⚠️ **走真实图，不在评测器里重搭一条链路**（M2 已定，理由照旧）：
   重搭的那份迟早和 `builder.py` 漂移，漂移之后指标照样好看、只是不再反映线上行为。

⚠️ **评测不写 `qa_logs`**：结果只落 `eval_case_results`。这样
   `stats/overview.qa_count` 天然不含评测轮次，不需要额外过滤。

⚠️ **与线上问答共用同一个 GPU 信号量**（§10.2 M5-2）：评测走的是
   `reranker.rerank()`，它内部用 `reranker._get_semaphore()` —— 与线上同一把锁，
   **不做优先级抢占**（评测是后台任务，慢一点没关系，抢显存会连线上一起搞崩）。

## 单轮 / 多轮（§6.3）

- `dialogue_mode="single"`：普通题跑 `question`；多轮题只跑 `turns[0]`，
  这样同一套题可以公平对比单轮/多轮。
- `dialogue_mode="multi"`：带 `turns` 的用例逐轮回放，累加 history / last_route，
  并用已回放轮次现算 `clarify_chain` / `clarify_total`（评测器没有 DB 历史）。
  **评测不跑滚动压缩** —— 2~4 轮历史可控，这是与线上的已知差异。

逐轮结果写进 `metrics["turns"]`；RAGAS 用 `sample_map` 把每条样本回填到
`(case, turn)`，不能像单轮那样只按用例下标回填（一条多轮用例有多条样本）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from app import db
from app.core import trace
from app.graph.builder import get_graph
# ★ 与模型看到的上下文**同一份渲染实现** —— 评测器绝不另抄一份
from app.graph.nodes.build_context import render_chunk_block
from app.graph.state import Message, UserContextLite, new_state
from app.eval import ragas as ragas_service

logger = logging.getLogger(__name__)

#: 检索指标看前几名
TOP_K = 5

RAGAS_KEYS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")


async def _drive_to_end(state: dict) -> dict:
    """把图跑到 END，返回最终状态。

    ⚠️ M5 要算 ragas 的忠实度与答案相关性，**必须有答案**，所以这次要跑到底。
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


def _first_turn(case: dict) -> dict | None:
    turns = case.get("turns")
    return turns[0] if isinstance(turns, list) and turns else None


def _case_expected_ids(case: dict) -> set[str]:
    raw = case.get("expected_doc_ids")
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return set(raw or [])


def _retrieval_rank(chunks: list, expected_ids: set[str],
                    expected_title: str | None) -> int | None:
    """返回第几名命中（1 起）→ 0 表示有目标但没命中 → None 表示没有目标。

    ⚠️ 「有期望但没召回」必须记 0，不能记 None：0 是真的失败，
       None 是「这题没有期望文档」。两者混在一起会让召回率虚高。
    """
    if expected_title:
        return next((i for i, c in enumerate(chunks, start=1)
                     if (c.document_name or "") == expected_title), 0)
    if expected_ids:
        return next((i for i, c in enumerate(chunks, start=1)
                     if c.document_id in expected_ids), 0)
    return None


def _expected_target(case: dict, turn: dict | None) -> tuple[set[str], str | None]:
    title = (turn or {}).get("expected_document") or None
    ids = _case_expected_ids(case)
    # 多轮题 case 级 expected_doc_ids 通常是空；单轮模式用 turns[0].expected_document。
    # 标题存在时优先按标题比较（文档 id 可能会变）。
    return (set() if title else ids), title


def _case_metrics(final: dict, case: dict, visibility: dict[str, set[str]],
                  role: str, *, expected_ids: set[str] | None = None,
                  expected_title: str | None = None) -> dict:
    """单题的**自定义**指标（ragas 四项另外算）。"""
    chunks = _rank_chunks(final)[:TOP_K]
    doc_ids = [c.document_id for c in chunks]

    if expected_ids is None:
        expected_ids = _case_expected_ids(case)
    first_hit = _retrieval_rank(chunks, expected_ids, expected_title)

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


def _turn_row(final: dict, turn: dict, *, index: int,
              expected_ids: set[str], expected_title: str | None) -> dict:
    """一轮的结构指标 + 引用指标。RAGAS 分数稍后回填。"""
    chunks = _rank_chunks(final)[:TOP_K]
    report = final.get("verify_report") or {}
    if hasattr(report, "model_dump"):
        report = report.model_dump()

    route = final.get("route") or ""
    expected_route = turn.get("expected_route")
    if expected_route is None and turn.get("should_clarify") is not None:
        expected_route = "clarify" if int(turn["should_clarify"]) else "knowledge"
    should = turn.get("should_clarify")
    anchors = turn.get("resolved_must_contain_any") or []
    resolved = final.get("resolved_query") or ""
    rank = _retrieval_rank(chunks, expected_ids, expected_title)

    return {
        "turn": index,
        "question": turn.get("question") or "",
        "resolved_query": resolved,
        "route": route,
        "route_ok": (route == expected_route) if expected_route else None,
        "clarify_ok": ((route == "clarify") == bool(int(should)))
                      if should is not None else None,
        "resolved_anchor_hit": (any(a in resolved for a in anchors)
                                if anchors else None),
        "retrieval_hit": (bool(rank) if rank is not None else None),
        "top1_doc": ((chunks[0].document_name or chunks[0].document_id)
                     if chunks else "(空)"),
        "evidence_ids": ([c.chunk_id for c in (final.get("evidence") or [])]
                         if (expected_title or expected_ids) else None),
        "citation_support_rate": report.get("citation_support_rate"),
        "unsupported_citations": len(report.get("unsupported_citations") or []),
        "unsupported_markers": len(report.get("unsupported_markers") or []),
        "refused": bool(final.get("refused")),
        "answer": (final.get("answer") or "")[:2000],
    }


async def _run_multi_case(case: dict, *, run_id: str, role: str,
                          include_restricted: bool, config: dict,
                          samples: list[dict], sample_map: list[tuple[int, int]],
                          result_index: int) -> dict:
    """逐轮回放一条用例；带 turns 的按 turns，不带的按单轮跑 1 轮。"""
    turns = case.get("turns") or [{
        "question": case["question"],
        "ground_truth": case.get("ground_truth"),
    }]
    session_id = f"eval-{run_id[:8]}-{case['id']}"
    history: list[Message] = []
    last_route = ""
    routes: list[str] = []
    turn_rows: list[dict] = []
    first_answer = ""
    t0 = time.perf_counter()

    for i, turn in enumerate(turns, start=1):
        # 生产环境从 messages.route 数；评测器没有 DB 历史，必须从已回放轮次现算。
        clarify_chain = 0
        for r in reversed(routes):
            if r != "clarify":
                break
            clarify_chain += 1
        clarify_total = sum(1 for r in routes if r == "clarify")

        state = new_state(
            query=turn.get("question") or "",
            session_id=session_id,
            user=UserContextLite(id="eval", role=role),
            history=list(history),
            last_route=last_route,
            clarify_chain=clarify_chain,
            clarify_total=clarify_total,
            include_restricted=include_restricted,
            eval_config=config if isinstance(config, dict) else {},
        )
        final = await _drive_to_end(state)

        expected_ids, expected_title = _expected_target(case, turn)
        row = _turn_row(final, turn, index=i, expected_ids=expected_ids,
                        expected_title=expected_title)
        turn_rows.append(row)

        route = row["route"]
        answer = final.get("answer") or ""
        if not first_answer:
            first_answer = answer

        contexts = [render_chunk_block(c, idx)
                    for idx, c in enumerate(final.get("evidence") or [], start=1)]
        if (route == "knowledge" and not bool(final.get("refused"))
                and answer and contexts):
            samples.append({
                "user_input": row["resolved_query"] or turn.get("question") or "",
                "response": answer,
                "retrieved_contexts": contexts,
                "reference": turn.get("ground_truth") or None,
            })
            sample_map.append((result_index, i - 1))

        # 与线上一致：进 history 的助手回答剥掉 [n]（生成节点也会再剥一次）
        history.append(Message(role="user", content=turn.get("question") or ""))
        history.append(Message(role="assistant",
                               content=re.sub(r"\[\d+\]", "", answer)))
        routes.append(route)
        last_route = route

    return {
        "case_id": case["id"],
        "metrics": {
            "turns": turn_rows,
            "turns_expected": len(turns),
            "turns_completed": len(turn_rows),
            "turns_with_reference": sum(
                1 for t in turns if str(t.get("ground_truth") or "").strip()),
            "ms": int((time.perf_counter() - t0) * 1000),
        },
        "answer": first_answer,
        "question": case["question"],
        "ground_truth": case.get("ground_truth"),
        "retrieved_ids": [],
        "unauthorized_hits": 0,
    }


async def _select_cases(conn, *, set_id: str | None, config: dict) -> list:
    """选题优先级（§5.5）。**分支顺序有意义，别调换。**

        set_id 给了        → WHERE set_id = $1 AND in_eval = TRUE     ← 新页面（批量评测）
        case_ids 给了      → WHERE id = ANY($1)                       ← 调试用
        case_type 给了     → WHERE case_type = $1                     ← ACL 对照实验用
        suite 给了         → 保持原 SQL，**不碰集合**                 ← 决策 22

    ⚠️ `suite` 路径一行 SQL 都不动：`suite='full'` 跑的是全部题（含校准题），
       一旦题量变了，消融表的历史行与新行题集就不同了。
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
    """推进度（§3.4）。"""
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
    with trace.begin_trace():
        with trace.trace_span("eval.run", layer="eval", run_id=run_id) as stage:
            result = await _run_eval(run_id)
            stage.set(**result)


async def _run_eval(run_id: str) -> dict:
    started = time.time()
    try:
        async with db.tx() as conn:
            run = await conn.fetchrow("SELECT * FROM eval_runs WHERE id = $1", run_id)
            if run is None:
                logger.error("评测 run 不存在",
                             extra={"event": "eval.missing_run", "run_id": run_id})
                return {"status": "missing"}
            await conn.execute(
                "UPDATE eval_runs SET status='running', started_at=now() WHERE id=$1",
                run_id)

        role = run["role"] or "student"
        include_restricted = bool(run["include_restricted"])
        dialogue_mode = run["dialogue_mode"] or "single"
        set_id = run["set_id"]
        config = run["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)

        async with db.tx() as conn:
            rows = await _select_cases(conn, set_id=set_id, config=config)
            visibility = await _load_visibility(conn)

        if not rows:
            error = ("这个评测集没有参与评测的用例"
                     if set_id else "no_cases")
            async with db.tx() as conn:
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), "
                    "metrics=$2, error=$3, total_cases=0, done_cases=0 WHERE id=$1",
                    run_id, {"error": error}, error)
            logger.warning("评测没有可跑的用例",
                           extra={"event": "eval.no_cases", "set_id": set_id})
            return {"status": "failed", "error": error, "cases": 0}

        await _record_progress(run_id, 0, len(rows))

        results: list[dict] = []
        ragas_samples: list[dict] = []
        ragas_index: list[int] = []
        sample_map: list[tuple[int, int]] = []

        for case in rows:
            case = dict(case)
            t0 = time.perf_counter()
            with trace.trace_span("eval.case", layer="eval", level=logging.DEBUG,
                                  case_id=case["id"],
                                  dialogue_mode=dialogue_mode) as case_stage:
                try:
                    if dialogue_mode == "multi":
                        # 本用例的样本先放局部列表：中途某轮抛异常时整条用例
                        # 不进 results，样本也不能留下 —— 否则 sample_map 会指向
                        # 一个没有 metrics 的失败结果，RAGAS 回填时 KeyError。
                        local_samples: list[dict] = []
                        local_map: list[tuple[int, int]] = []
                        result = await _run_multi_case(
                            case, run_id=run_id, role=role,
                            include_restricted=include_restricted, config=config,
                            samples=local_samples, sample_map=local_map,
                            result_index=len(results))
                        ragas_samples.extend(local_samples)
                        sample_map.extend(local_map)
                    else:
                        result = await _run_single_case(
                            case, run_id=run_id, role=role,
                            include_restricted=include_restricted, config=config,
                            visibility=visibility, samples=ragas_samples,
                            sample_index=len(results))
                except Exception as e:  # noqa: BLE001 —— 单题失败不该拖垮整轮
                    logger.warning("评测用例执行失败：%s", e,
                                   extra={"event": "eval.case_failed",
                                          "case_id": case["id"],
                                          "dialogue_mode": dialogue_mode})
                    results.append({"case_id": case["id"],
                                    "error": f"{type(e).__name__}: {e}",
                                    "question": case["question"],
                                    "ground_truth": case.get("ground_truth")})
                    case_stage.set(error=type(e).__name__)
                    await _record_progress(run_id, len(results))
                    continue
                case_stage.set(route=(result.get("metrics", {}).get("route")
                                      or (result.get("metrics", {}).get("turns") or [{}])[0].get("route", "")))
            elapsed = int((time.perf_counter() - t0) * 1000)
            result["metrics"]["ms"] = elapsed
            if dialogue_mode == "single":
                # 单轮 RAGAS 样本下标记的是 result 的下标
                ragas_index.append(len(results))
            results.append(result)
            await _record_progress(run_id, len(results))

        ragas = await ragas_service.score_samples(ragas_samples) if ragas_samples else {
            "available": True, "ok": True, "rows": [], "means": {}, "errors": []}
        scored_rows = ragas.get("rows") or []
        if dialogue_mode == "multi":
            for (pos, turn_idx), row in zip(sample_map, scored_rows):
                results[pos]["metrics"]["turns"][turn_idx].update(
                    {f"ragas_{k}": v for k, v in row.items()})
        else:
            for pos, row in zip(ragas_index, scored_rows):
                results[pos]["metrics"].update(
                    {f"ragas_{k}": v for k, v in row.items()})

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
                    r.get("question"), r.get("ground_truth"), r.get("error"),
                )

            summary = _aggregate(results, ragas, dialogue_mode)
            await conn.execute(
                "UPDATE eval_runs SET status='done', finished_at=now(), metrics=$2, "
                "done_cases=$3 WHERE id=$1",
                run_id, summary, len(results))

        return {"status": "done", "cases": len(results),
                "ms": int((time.time() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        logger.exception("评测整轮失败", extra={
            "event": "eval.failed", "stage": "eval.run", "layer": "eval",
            "run_id": run_id, "error": type(e).__name__})
        try:
            async with db.tx() as conn:
                why = f"{type(e).__name__}: {e}"
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), metrics=$2, "
                    "error=$3 WHERE id=$1", run_id, {"error": why}, why)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "error": type(e).__name__}


async def _run_single_case(case: dict, *, run_id: str, role: str,
                           include_restricted: bool, config: dict,
                           visibility: dict[str, set[str]], samples: list[dict],
                           sample_index: int) -> dict:
    """单轮模式：普通题跑 question；多轮题只跑 turns[0]。"""
    turn = _first_turn(case)
    question = turn.get("question") if turn else case["question"]
    ground_truth = ((turn.get("ground_truth") if turn else None)
                    or case.get("ground_truth"))
    expected_ids, expected_title = _expected_target(case, turn)

    state = new_state(
        query=question,
        session_id=f"eval-{run_id[:8]}",
        user=UserContextLite(id="eval", role=role),
        history=[],
        include_restricted=include_restricted,
        eval_config=config if isinstance(config, dict) else {},
    )
    final = await _drive_to_end(state)
    metrics = _case_metrics(final, case, visibility, role,
                            expected_ids=expected_ids,
                            expected_title=expected_title)
    metrics["dialogue_mode"] = "single"

    answer = final.get("answer") or ""
    contexts = [render_chunk_block(c, i)
                for i, c in enumerate(final.get("evidence") or [], start=1)]
    if (metrics.get("route") == "knowledge" and answer and contexts
            and not metrics.get("refused")):
        samples.append({
            "user_input": final.get("resolved_query") or question,
            "response": answer,
            "retrieved_contexts": contexts,
            "reference": ground_truth or None,
        })
    return {
        "case_id": case["id"], "metrics": metrics, "answer": answer,
        "retrieved_ids": [c.chunk_id for c in _rank_chunks(final)[:TOP_K]],
        "unauthorized_hits": metrics["unauthorized_hits"],
        "question": question,
        "ground_truth": ground_truth,
    }


def _avg(ok: list[dict], key: str):
    vals = [r["metrics"][key] for r in ok
            if isinstance(r["metrics"].get(key), (int, float))
            and not isinstance(r["metrics"].get(key), bool)]
    return round(sum(vals) / len(vals), 4) if vals else None


def _aggregate(results: list[dict], ragas: dict,
               dialogue_mode: str = "single") -> dict:
    """整轮汇总：自定义指标求均值 + ragas 四指标取均值。"""
    ok = [r for r in results if "metrics" in r]

    answered = [r for r in ok if not r["metrics"].get("refused")]
    with_gt = [r for r in ok if r["metrics"].get("rank") is not None
               and r["metrics"].get("rank", 0) >= 0]

    summary: dict[str, Any] = {
        "cases": len(results),
        "failed": len(results) - len(ok),
        "recall_at_k": _avg(ok, "recall_at_k"),
        "mrr": _avg(ok, "mrr"),
        "avg_ms": _avg(ok, "ms"),
        "refused_count": sum(1 for r in ok if r["metrics"].get("refused")),
        "unauthorized_hits": sum(int(r["metrics"].get("unauthorized_hits") or 0)
                                 for r in ok),
        "uncited_ratio": _avg(ok, "uncited_ratio"),
        "citation_invalid": sum(int(r["metrics"].get("citation_invalid") or 0)
                                for r in ok),
        "ragas": ragas.get("means") or {},
        "ragas_available": bool(ragas.get("available")),
        "ragas_errors": ragas.get("errors") or [],
        "ragas_scored_cases": sum(
            1 for r in (ragas.get("rows") or [])
            if any(v is not None for v in r.values())),
    }
    knowledge = [r for r in ok if r["metrics"].get("route") == "knowledge"]
    summary["knowledge_count"] = len(knowledge)
    summary["refusal_rate"] = (
        round(sum(1 for r in knowledge if r["metrics"].get("refused")) / len(knowledge), 4)
        if knowledge else None)
    summary["answered_count"] = len(answered)
    summary["scored_with_ground_truth"] = len(with_gt)

    if dialogue_mode != "multi":
        return summary

    turns = [t for r in ok for t in (r["metrics"].get("turns") or [])]

    def _mean(vals):
        vals = [v for v in vals
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _rate(key: str):
        vals = [t[key] for t in turns if isinstance(t.get(key), bool)]
        return _mean([1.0 if v else 0.0 for v in vals])

    def _scored(turn: dict) -> bool:
        return any(turn.get(f"ragas_{k}") is not None for k in RAGAS_KEYS)

    scored_cases = sum(
        1 for r in ok
        if any(_scored(t) for t in (r["metrics"].get("turns") or [])))
    scored_turns = sum(1 for t in turns if _scored(t))

    summary.update({
        "turns_total": sum(int(r["metrics"].get("turns_expected") or 0) for r in ok),
        "turns_completed": sum(int(r["metrics"].get("turns_completed") or 0)
                               for r in ok),
        "turns_with_reference": sum(
            int(r["metrics"].get("turns_with_reference") or 0) for r in ok),
        "route_accuracy": _rate("route_ok"),
        "clarify_accuracy": _rate("clarify_ok"),
        "retrieval_hit_rate": _rate("retrieval_hit"),
        "resolve_anchor_hit_rate": _rate("resolved_anchor_hit"),
        "citation_support_rate": _mean(
            [t.get("citation_support_rate") for t in turns]),
        "unsupported_citations": sum(int(t.get("unsupported_citations") or 0)
                                     for t in turns),
        "unsupported_markers": sum(int(t.get("unsupported_markers") or 0)
                                   for t in turns),
        "ragas_scored_turns": scored_turns,
        "ragas_scored_cases": scored_cases,
        "ragas": {k: _mean([t.get(f"ragas_{k}") for t in turns])
                  for k in RAGAS_KEYS},
        "knowledge_count": sum(1 for t in turns if t.get("route") == "knowledge"),
        "refused_count": sum(1 for t in turns if t.get("refused")),
        "answered_count": sum(
            1 for t in turns
            if t.get("route") == "knowledge" and not t.get("refused")),
    })
    summary["refusal_rate"] = (
        round(summary["refused_count"] / summary["knowledge_count"], 4)
        if summary["knowledge_count"] else None)
    summary["multi_turn"] = {
        k: summary[k] for k in (
            "turns_total", "turns_completed", "turns_with_reference",
            "route_accuracy", "clarify_accuracy", "retrieval_hit_rate",
            "resolve_anchor_hit_rate", "citation_support_rate",
            "unsupported_citations", "unsupported_markers",
        )
    }
    return summary
