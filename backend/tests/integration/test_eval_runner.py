"""跑一轮评测（§5.5 / §3.4 / §7.6）—— 选题、进度、错误原因。

这里不真跑图（那要 LLM + 模型，分钟级且花钱）：把 `_drive_to_end` 换掉，
只验**选题、计数、落库**这三件评测器自己负责的事。

四条最容易做错、且错了看不出来的：

① **`set_id` 路径只看 `in_eval=true`** —— 这就是界面上那个开关的全部意义。
   而 `suite` 路径**不许**过滤 `in_eval`：CI 的校准题集必须稳定，
   有人在界面上随手关掉一条，不该把 CI 门禁的分母改了。

② **`suite='full'` 仍然跑全部 90 条**（决策 22），不是「默认题库」的 75 条。
   一旦变成 75，消融表的历史行与新行题集就不同了，差值不再只反映配置差异。

③ **没有 `expected_doc_ids` 的题，召回率记 `None` 而不是 0**（§7.6）。
   记 0 的话，手工录入的题会**凭空拉低**整轮的 Recall@5 / MRR，
   而且逐题单看都"正常"，查不出原因。

④ **单题失败要留下原因**（§1.1③ 那个 bug：原来 `error` 写库时被丢掉了）。
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from app import db
from app.eval import runner
from app.graph.state import Chunk


@pytest_asyncio.fixture
async def scratch_set():
    set_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute("INSERT INTO eval_sets (id, name) VALUES ($1,$2)",
                           set_id, f"跑测集-{set_id[:8]}")
    yield set_id
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


@pytest.fixture(autouse=True)
def no_ragas(monkeypatch):
    """ragas 走隔离环境的子进程，测试里不启 —— 只记「不可用」。"""
    async def unavailable(samples, *a, **kw):
        return {"available": False, "ok": False, "rows": [], "means": {},
                "errors": ["测试环境不跑 ragas"], "ms": 0}
    monkeypatch.setattr(runner.ragas_service, "score_samples", unavailable)


def _final(doc_ids: list[str], *, answer: str = "生成的答案",
           refused: bool = False) -> dict:
    chunks = [Chunk(chunk_id=f"c{i}", text=f"片段{i}", document_id=d)
              for i, d in enumerate(doc_ids)]
    return {"reranked": chunks, "answer": answer, "refused": refused,
            "route": "knowledge", "decision": "answer", "verify_report": {}}


def _graph_returning(monkeypatch, finals: dict[str, dict]):
    """按题面挑一个假答案 —— 让每道题有不同的检索结果。"""
    async def fake(state):
        return finals[state["query"]]
    monkeypatch.setattr(runner, "_drive_to_end", fake)


async def _make_run(**cols) -> str:
    run_id = uuid.uuid4().hex
    fields = {"id": run_id, "name": "t", "config": "{}", "role": "student",
              "include_restricted": 0, "status": "pending"}
    fields.update(cols)
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted, status,
                                      set_id, set_name)
               VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7,$8)""",
            fields["id"], fields["name"], json.dumps(fields["config"]),
            fields["role"], fields["include_restricted"], fields["status"],
            fields.get("set_id"), fields.get("set_name"))
    return run_id


async def _run_row(run_id: str) -> dict:
    async with db.tx() as conn:
        return dict(await conn.fetchrow("SELECT * FROM eval_runs WHERE id=$1", run_id))


async def _results(run_id: str) -> list[dict]:
    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT * FROM eval_case_results WHERE run_id=$1 ORDER BY case_id", run_id)
    out = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d["metrics"]) if isinstance(d["metrics"], str) else d["metrics"]
        out.append(d)
    return out


async def _add_case(conn, set_id, question, *, in_eval=True, expected=None,
                    suite="full"):
    case_id = uuid.uuid4().hex
    await conn.execute(
        """INSERT INTO eval_cases (id, set_id, question, ground_truth, case_type, suite,
                                   source, in_eval, expected_doc_ids)
           VALUES ($1,$2,$3,'标准答案','factual',$4,'manual',$5,$6)""",
        case_id, set_id, question, suite, in_eval,
        json.dumps(expected) if expected else None)
    return case_id


# ============================================================
# 选题（§5.5）
# ============================================================

async def test_set_path_runs_only_in_eval_cases(scratch_set, monkeypatch):
    """`in_eval=false` 不进这一轮 —— 「参与评测」开关因此真正生效。"""
    _graph_returning(monkeypatch, {"要跑的": _final(["d1"]), "不跑的": _final(["d1"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "要跑的")
        await _add_case(conn, scratch_set, "不跑的", in_eval=False)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        row = await _run_row(run_id)
        assert row["status"] == "done"
        assert row["total_cases"] == 1, "只该选中 in_eval=true 的那条"
        assert [r["question"] for r in await _results(run_id)] == ["要跑的"]
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_suite_path_ignores_in_eval(scratch_set, monkeypatch):
    """⚠️ 代价如实写明：同一道题在批量评测页关掉后，消融/CI 里照样跑（§5.5）。

    理由：那些入口的题集必须稳定 —— 有人在界面上随手关掉一条校准题，
    就把 CI 门禁的分母改了，甚至可能直接空集导致 CI 变红。
    """
    async with db.tx() as conn:
        case_id = await _add_case(conn, scratch_set, "校准题", in_eval=False,
                                  suite="refusal_calib")
    _graph_returning(monkeypatch, {"校准题": _final(["d1"])})
    run_id = await _make_run(config={"suite": "refusal_calib"})
    try:
        await runner.run_eval(run_id)
        rows = await _results(run_id)
        mine = [r for r in rows if r["case_id"] == case_id]
        assert mine, "suite 路径不该看 in_eval —— 它必须照跑"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_suite_full_still_selects_all_ninety_cases(monkeypatch):
    """决策 22：`suite='full'` 仍选中**全部 90 条**，不是「默认题库」的 75 条。"""
    monkeypatch.setattr(runner, "_drive_to_end",
                        lambda state: _final([]))          # 不实际跑图
    async with db.tx() as conn:
        total = await conn.fetchval("SELECT count(*) FROM eval_cases")
        full = await conn.fetchval(
            "SELECT count(*) FROM eval_cases WHERE suite='full'")
        picked = await runner._select_cases(conn, set_id=None, config={"suite": "full"})
    assert len(picked) == total == 90
    assert len(picked) > full, "§full 路径必须比「默认题库」的 75 条多"


async def test_empty_set_fails_with_an_explanation(scratch_set, monkeypatch):
    """选到空集 → 直接标 failed，**不跑出一个空报告**（§5.5）。"""
    run_id = await _make_run(set_id=scratch_set, set_name="空集")
    try:
        await runner.run_eval(run_id)
        row = await _run_row(run_id)
        assert row["status"] == "failed"
        assert "用例" in (row["error"] or ""), row["error"]
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


# ============================================================
# 进度与错误原因（§3.4 / §1.1③）
# ============================================================

async def test_done_cases_counts_up_to_total(scratch_set, monkeypatch):
    """进度：`done_cases` 从 0 递增到 `total_cases`（界面上的 `0/5`）。"""
    seen: list[int] = []
    original = runner._record_progress

    async def spy(run_id, done, total=None):
        seen.append(done)          # 记**每一次**推进，包括设定 total 的那一次
        return await original(run_id, done, total)

    monkeypatch.setattr(runner, "_record_progress", spy)
    _graph_returning(monkeypatch, {f"问题{i}": _final(["d1"]) for i in range(3)})
    async with db.tx() as conn:
        for i in range(3):
            await _add_case(conn, scratch_set, f"问题{i}")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        row = await _run_row(run_id)
        assert row["total_cases"] == 3
        assert row["done_cases"] == 3, "跑完了进度必须追平总数"
        # 开工时先写 total（done=0），此后每跑完一题 +1
        assert seen == [0, 1, 2, 3], f"进度不是逐题递增：{seen}"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_single_case_failure_records_the_reason(scratch_set, monkeypatch):
    """§1.1③ 那个 bug：单题抛异常时，原因要**真的落库**。"""
    async def fake(state):
        if state["query"] == "会炸的题":
            raise RuntimeError("检索炸了")
        return _final(["d1"])
    monkeypatch.setattr(runner, "_drive_to_end", fake)

    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "会炸的题")
        await _add_case(conn, scratch_set, "正常的题")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        rows = await _results(run_id)
        broken = next(r for r in rows if r["question"] == "会炸的题")
        assert broken["error"] and "检索炸了" in broken["error"]
        healthy = next(r for r in rows if r["question"] == "正常的题")
        assert not healthy["error"], "没出错的不该有失败原因（拒答也不算失败）"
        # 整轮还是要跑完
        assert (await _run_row(run_id))["status"] == "done"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_results_carry_question_and_ground_truth_snapshots(scratch_set, monkeypatch):
    """快照：跑的时候把该题的问题与标准答案抄一份进来，历史报告才自给自足。"""
    _graph_returning(monkeypatch, {"问题": _final(["d1"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "问题")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        row = (await _results(run_id))[0]
        assert row["question"] == "问题" and row["ground_truth"] == "标准答案"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


# ============================================================
# 召回率不被手工题拉低（§7.6）
# ============================================================

async def test_manual_case_without_expected_docs_does_not_drag_recall_down(
        scratch_set, monkeypatch):
    """一条有期望文档且命中的题 + 一条没有期望文档的题 → 召回率是 **1.0 不是 0.5**。

    没有 `expected_doc_ids` 的题（界面手工录入的都没有）算不出排名，
    记 `None` 而不是 0 —— `_avg()` 的数值过滤会自动跳过它。
    """
    _graph_returning(monkeypatch, {"有期望的题": _final(["d-hit"]),
                                   "手工录入的题": _final(["d-hit"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "有期望的题", expected=["d-hit"])
        await _add_case(conn, scratch_set, "手工录入的题")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        row = await _run_row(run_id)
        metrics = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else row["metrics"]
        assert metrics["recall_at_k"] == 1.0, metrics["recall_at_k"]
        assert metrics["mrr"] == 1.0
        # 「有参考答案的题数」也要跟着排除，否则会虚高
        assert metrics["scored_with_ground_truth"] == 1

        rows = {r["question"]: r for r in await _results(run_id)}
        assert rows["手工录入的题"]["metrics"]["rank"] is None
        assert rows["手工录入的题"]["metrics"]["recall_at_k"] is None
        assert rows["有期望的题"]["metrics"]["rank"] == 1
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_expected_but_missed_still_scores_zero(scratch_set, monkeypatch):
    """⚠️ 别把「没有期望」与「没召回」混为一谈：有期望但没命中，**就是 0**。"""
    _graph_returning(monkeypatch, {"没命中的题": _final(["d-other"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "没命中的题", expected=["d-want"])
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        metrics = (await _run_row(run_id))["metrics"]
        metrics = json.loads(metrics) if isinstance(metrics, str) else metrics
        assert metrics["recall_at_k"] == 0.0
        assert metrics["scored_with_ground_truth"] == 1
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)
