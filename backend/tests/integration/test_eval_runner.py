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
import logging
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
           refused: bool = False, document_name: str = "") -> dict:
    chunks = [Chunk(chunk_id=f"c{i}", text=f"片段{i}", document_id=d,
                    document_name=document_name)
              for i, d in enumerate(doc_ids)]
    # 真图里 build_context 会把编号后的 `evidence` 写进 state —— 假图照做，
    # 评测器取的正是它（`retrieved_contexts` 来自 `final["evidence"]`，不是 reranked）
    return {"reranked": chunks, "evidence": chunks, "answer": answer, "refused": refused,
            "route": "knowledge", "decision": "answer", "verify_report": {}}


def _graph_returning(monkeypatch, finals: dict[str, dict]):
    """按题面挑一个假答案 —— 让每道题有不同的检索结果。"""
    async def fake(state):
        return finals[state["query"]]
    monkeypatch.setattr(runner, "_drive_to_end", fake)


async def _make_run(**cols) -> str:
    run_id = uuid.uuid4().hex
    fields = {"id": run_id, "name": "t", "config": "{}", "role": "student",
              "include_restricted": 0, "status": "pending",
              "dialogue_mode": "single"}
    fields.update(cols)
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted, status,
                                      set_id, set_name, dialogue_mode)
               VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9)""",
            fields["id"], fields["name"], json.dumps(fields["config"]),
            fields["role"], fields["include_restricted"], fields["status"],
            fields.get("set_id"), fields.get("set_name"), fields["dialogue_mode"])
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
        expected)          # 原值 —— 连接上已注册 jsonb 编解码器，别再 dumps
    return case_id


async def _add_turn_case(conn, set_id, *, turns, question=None):
    case_id = uuid.uuid4().hex
    await conn.execute(
        """INSERT INTO eval_cases (id, set_id, question, ground_truth, case_type, suite,
                                   source, in_eval, turns)
           VALUES ($1,$2,$3,NULL,'multi_turn','full','manual',TRUE,$4)""",
        case_id, set_id, question or turns[0]["question"], turns)
    return case_id


# ============================================================
# 选题（§5.5）
# ============================================================

async def test_run_eval_emits_run_and_case_logs(scratch_set, monkeypatch, caplog):
    """一轮评测至少要有 run 汇总与单题阶段日志。"""
    _graph_returning(monkeypatch, {"日志题": _final(["d1"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "日志题")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            await runner.run_eval(run_id)

        stages = {getattr(r, "stage", "") for r in caplog.records}
        assert "eval.run" in stages, f"缺少 eval.run：{sorted(stages)}"
        assert "eval.case" in stages, f"缺少 eval.case：{sorted(stages)}"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


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


async def test_suite_full_still_selects_every_case(monkeypatch):
    """决策 22：`suite='full'` 仍选中**全表**用例，不是「默认题库」那一批。

    ⚠️ **不要写死 90 这个数。** 评测集管理页现在能直接加用例了，
       写死数字会让这个测试在任何人加过题之后无缘无故变红 ——
       而它真正要守的是「`suite` 路径不碰集合」，不是「库里恰好有 90 条」。
    """
    monkeypatch.setattr(runner, "_drive_to_end",
                        lambda state: _final([]))          # 不实际跑图
    async with db.tx() as conn:
        total = await conn.fetchval("SELECT count(*) FROM eval_cases")
        default_set = await conn.fetchval(
            "SELECT count(*) FROM eval_cases WHERE set_id='eval-set-default'")
        picked = await runner._select_cases(conn, set_id=None, config={"suite": "full"})
    assert len(picked) == total, "suite='full' 要选全表"
    assert len(picked) > default_set, "必须比「默认题库」那一批多（多出来的正是校准题）"


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
    _graph_returning(monkeypatch, {f"问题{i}": _final(["d1"]) for i in range(3)})
    async with db.tx() as conn:
        for i in range(3):
            await _add_case(conn, scratch_set, f"问题{i}")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")

    # ⚠️ 先建好 run 再装探针，**并且只记这一轮**：
    #    `POST /run` 是 `asyncio.create_task` 起的后台任务，接口测试跑完就删了行，
    #    但那条任务还在继续推进度 —— 探针装在模块上，会把它的一起记进来
    #    （实测见过 `[11, 12, 13, 0, 14, 1, …]` 这种两轮交错的序列）。
    seen: list[int] = []
    original = runner._record_progress

    async def spy(rid, done, total=None):
        if rid == run_id:
            seen.append(done)      # 记**每一次**推进，包括设定 total 的那一次
        return await original(rid, done, total)

    monkeypatch.setattr(runner, "_record_progress", spy)
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


# ============================================================
# 失败原因必须写进「失败原因」列（不是只塞进 metrics）
# ============================================================
# ⚠️ `eval_runs` 有**两个**装原因的地方：`error` 列（界面的「失败原因」列读它）
#    和 `metrics.error`。实测踩到：库里 4 个 failed 轮次，`metrics.error` 里
#    写着「stale: 上次进程退出时未结束」，而 `error` 列是空的 ——
#    于是界面上显示「失败」，右边的失败原因却是空白，等于没给原因。

async def test_whole_run_crash_writes_the_error_column(scratch_set, monkeypatch):
    """整轮抛异常时，原因要落到 `error` 列（界面读的那一列）。"""
    async def boom(*a, **kw):
        raise RuntimeError("检索炸了")
    monkeypatch.setattr(runner, "_select_cases", boom)

    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)
        row = await _run_row(run_id)
        assert row["status"] == "failed"
        assert row["error"] and "检索炸了" in row["error"], \
            f"「失败原因」列是空的（只有 metrics.error）：{row['error']!r}"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_stale_cleanup_writes_the_error_column():
    """服务重启时清理僵死轮次，原因同样要落到 `error` 列。"""
    from app.main import _fail_stale_evals

    run_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted, status)
               VALUES ($1,'僵死的','{}'::jsonb,'student',0,'running')""", run_id)
    try:
        await _fail_stale_evals()
        async with db.tx() as conn:
            row = await conn.fetchrow(
                "SELECT status, error, metrics FROM eval_runs WHERE id=$1", run_id)
        assert row["status"] == "failed"
        assert row["error"] and "stale" in row["error"], \
            f"「失败原因」列是空的（只有 metrics.error）：{row['error']!r}"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


# ============================================================
# 喂给 ragas 的样本（两块历史伤口的护栏）
# ============================================================
# ⚠️ 实测（2026-10-15 的 `第二课堂评分表专项` 那轮，库里 d0b270f4）：
#    ragas 拿到的是 `c.text`，**丢了模型看到的那行 `[n] 《文档名》 · 第 x 页`**；
#    而文档名是上传文件名，正文里一个字都没有 —— 模型照着头部写的
#    「根据《12_第二课堂成绩单制度实施办法…》」在 ragas 眼里全是无出处的话。
#    同一轮再把拒答（faithfulness / answer_relevancy 按构造 = 0）也塞了进去，
#    于是 answer_relevancy 10 题 0.5494、只看 6 道作答题 0.9157。

@pytest.fixture
def ragas_samples(monkeypatch):
    """把 ragas 换成可控假实现，并把**实际送进去的样本**抄一份出来。"""
    samples: list[dict] = []

    async def fake(batch, *a, **kw):
        samples.extend(batch)
        row = {"faithfulness": 1.0, "answer_relevancy": 1.0,
               "context_precision": 1.0, "context_recall": 1.0}
        return {"available": True, "ok": True, "rows": [dict(row) for _ in batch],
                "means": dict(row), "errors": [], "ms": 0}

    monkeypatch.setattr(runner.ragas_service, "score_samples", fake)
    return samples


async def test_ragas_contexts_are_what_the_model_saw(scratch_set, monkeypatch,
                                                     ragas_samples):
    """★ `retrieved_contexts` = 模型看到的上下文：同一份 evidence、同一个渲染函数。

    三件事一起钉住：① 头行（文档名 + 页码）必须在 —— 判官要能验证
    「根据《…》」这类话；② 顺序是 **evidence** 的顺序（编号顺序），不是精排顺序；
    ③ 渲染来自 `build_context.render_chunk_block` 这一份实现，不另抄一份。
    """
    doc_name = "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号"
    final = _final(["d1", "d2"], document_name=doc_name)
    # 编号顺序 ≠ 精排顺序 —— 真实图中 build_context 是分组重排后才编号的
    final["evidence"] = list(reversed(final["reranked"]))
    _graph_returning(monkeypatch, {"题": final})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "题")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)

        assert len(ragas_samples) == 1
        contexts = ragas_samples[0]["retrieved_contexts"]
        # ① 头行在、正文紧随其后 —— 判官看得到文档名
        assert contexts[0] == f"[1] 《{doc_name}》 · 第 1 页\n片段1\n"
        # ② 顺序跟着 evidence（片段1 在前），不是 reranked
        assert contexts[1] == f"[2] 《{doc_name}》 · 第 1 页\n片段0\n"
        # ③ 与 build_context 同一份渲染实现（抄第二份就会在这里漂移）
        from app.graph.nodes.build_context import render_chunk_block
        assert contexts == [
            render_chunk_block(c, i)
            for i, c in enumerate(final["evidence"], start=1)]
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_refused_cases_are_not_sent_to_ragas(scratch_set, monkeypatch,
                                                   ragas_samples):
    """拒答**不进 ragas**：它在 faithfulness / answer_relevancy 上按构造就是 0，
    送进去等于把「拒答率」混进「答案质量」的均值（实测 10 题 0.5494 vs
    6 道作答题 0.9157）。拒答仍由 `refused_count` / `refusal_rate` 单独报，
    而 `ragas_scored_cases` 把均值的**真实分母**记下来 —— 6 题算出来的均值
    不能被当成 10 题的读。
    """
    _graph_returning(monkeypatch, {
        "答得上的": _final(["d1"]),
        "答不上的": _final(["d1"], answer="知识库中未找到相关依据，无法回答该问题。",
                           refused=True),
    })
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "答得上的")
        await _add_case(conn, scratch_set, "答不上的")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)

        assert [s["user_input"] for s in ragas_samples] == ["答得上的"], \
            "拒答题不该送 ragas —— 它两项按构造得 0，会把均值整个拉下去"
        metrics = (await _run_row(run_id))["metrics"]
        metrics = json.loads(metrics) if isinstance(metrics, str) else metrics
        assert metrics["ragas_scored_cases"] == 1, "均值算在几道上要落在整轮汇总里"
        # 拒答在自己的口径里照旧：2 道 knowledge 题里拒了 1 道
        assert metrics["refused_count"] == 1
        assert metrics["refusal_rate"] == 0.5
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


# ============================================================
# 单轮 / 多轮执行（§6.3）
# ============================================================

async def test_single_mode_multi_turn_case_uses_first_turn(scratch_set, monkeypatch):
    turns = [
        {"question": "转专业需要什么条件？", "ground_truth": "第一轮答案",
         "expected_document": "转专业办法"},
        {"question": "那需要什么材料？", "ground_truth": "第二轮答案"},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    seen: list[str] = []

    async def fake(state):
        seen.append(state["query"])
        chunk = Chunk(chunk_id="c1", text="片段", document_id="d1",
                      document_name="转专业办法")
        return {"reranked": [chunk], "evidence": [], "answer": "答",
                "refused": False, "route": "knowledge", "decision": "ANSWERED",
                "verify_report": {"total_claims": 1, "cited_claims": 1}}

    monkeypatch.setattr(runner, "_drive_to_end", fake)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="single")
    try:
        await runner.run_eval(run_id)
        assert seen == ["转专业需要什么条件？"]
        row = (await _results(run_id))[0]
        assert row["question"] == "转专业需要什么条件？"
        assert row["ground_truth"] == "第一轮答案"
        assert row["metrics"]["rank"] == 1
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_multi_mode_replays_turns_with_history_and_clarify_counts(
        scratch_set, monkeypatch):
    turns = [
        {"question": "转专业需要什么条件？", "ground_truth": "条件A",
         "expected_route": "knowledge", "should_clarify": 0},
        {"question": "那需要什么材料？", "ground_truth": "材料B",
         "expected_route": "knowledge", "should_clarify": 0,
         "resolved_must_contain_any": ["材料"]},
        {"question": "多久？", "ground_truth": "时限C",
         "expected_route": "clarify", "should_clarify": 1},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    seen: list[dict] = []

    async def fake(state):
        seen.append({
            "query": state["query"],
            "history": list(state.get("history") or []),
            "last_route": state.get("last_route") or "",
            "clarify_chain": state.get("clarify_chain") or 0,
            "clarify_total": state.get("clarify_total") or 0,
        })
        route = "clarify" if state["query"] == "多久？" else "knowledge"
        answer = "澄清问题" if route == "clarify" else f"答：{state['query']}"
        return {"reranked": [], "evidence": [], "answer": answer,
                "refused": False, "route": route, "decision": "ANSWERED",
                "resolved_query": state["query"], "verify_report": {}}

    monkeypatch.setattr(runner, "_drive_to_end", fake)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="multi")
    try:
        await runner.run_eval(run_id)
        assert [s["query"] for s in seen] == [t["question"] for t in turns]
        assert seen[1]["history"][0].role == "user"
        assert seen[1]["history"][1].role == "assistant"
        assert seen[1]["last_route"] == "knowledge"
        assert seen[2]["last_route"] == "knowledge"
        assert seen[2]["clarify_chain"] == 0
        row = (await _results(run_id))[0]
        assert len(row["metrics"]["turns"]) == 3
        assert row["metrics"]["turns"][2]["route"] == "clarify"
        assert row["metrics"]["turns"][2]["clarify_ok"] is True
        assert row["metrics"]["turns_expected"] == 3
        assert row["metrics"]["turns_completed"] == 3
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_multi_mode_ragas_sample_map_fills_each_turn(
        scratch_set, monkeypatch):
    turns = [
        {"question": "第一问", "ground_truth": "答案一"},
        {"question": "第二问", "ground_truth": "答案二"},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    async def fake(state):
        return {"reranked": [], "evidence": [Chunk(chunk_id="c1", text="片段")],
                "answer": f"答{state['query']}", "refused": False,
                "route": "knowledge", "decision": "ANSWERED",
                "resolved_query": state["query"], "verify_report": {}}
    monkeypatch.setattr(runner, "_drive_to_end", fake)

    calls: dict[str, list] = {}

    async def score(batch, *a, **kw):
        calls["batch"] = batch
        rows = [{"faithfulness": 0.1, "answer_relevancy": 0.2,
                 "context_precision": None, "context_recall": None},
                {"faithfulness": 0.3, "answer_relevancy": 0.4,
                 "context_precision": None, "context_recall": None}]
        return {"available": True, "ok": True, "rows": rows, "means": {},
                "errors": [], "ms": 0}

    monkeypatch.setattr(runner.ragas_service, "score_samples", score)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="multi")
    try:
        await runner.run_eval(run_id)
        row = (await _results(run_id))[0]
        t1, t2 = row["metrics"]["turns"]
        assert t1["ragas_faithfulness"] == 0.1
        assert t2["ragas_faithfulness"] == 0.3
        assert [s["user_input"] for s in calls["batch"]] == ["第一问", "第二问"]
        assert calls["batch"][1]["reference"] == "答案二"
        summary = (await _run_row(run_id))["metrics"]
        summary = json.loads(summary) if isinstance(summary, str) else summary
        assert summary["ragas_scored_turns"] == 2
        assert summary["multi_turn"]["turns_total"] == 2
        assert summary["multi_turn"]["turns_with_reference"] == 2
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_multi_mode_skips_chat_and_refused_turns_in_ragas(
        scratch_set, monkeypatch):
    turns = [
        {"question": "闲聊", "ground_truth": "答"},
        {"question": "拒答", "ground_truth": "答"},
        {"question": "作答", "ground_truth": "答"},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    async def fake(state):
        q = state["query"]
        if q == "闲聊":
            return {"reranked": [], "evidence": [], "answer": "你好",
                    "refused": False, "route": "chat", "decision": "ANSWERED",
                    "resolved_query": q, "verify_report": {}}
        if q == "拒答":
            return {"reranked": [], "evidence": [Chunk(chunk_id="c1", text="片段")],
                    "answer": "没找到", "refused": True, "route": "knowledge",
                    "decision": "REFUSED_NO_EVIDENCE", "resolved_query": q,
                    "verify_report": {}}
        return {"reranked": [], "evidence": [Chunk(chunk_id="c1", text="片段")],
                "answer": "答", "refused": False, "route": "knowledge",
                "decision": "ANSWERED", "resolved_query": q, "verify_report": {}}
    monkeypatch.setattr(runner, "_drive_to_end", fake)
    samples: list[dict] = []

    async def score(batch, *a, **kw):
        samples.extend(batch)
        return {"available": True, "ok": True,
                "rows": [{"faithfulness": 1.0, "answer_relevancy": 1.0,
                          "context_precision": None, "context_recall": None}],
                "means": {}, "errors": [], "ms": 0}

    monkeypatch.setattr(runner.ragas_service, "score_samples", score)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="multi")
    try:
        await runner.run_eval(run_id)
        assert [s["user_input"] for s in samples] == ["作答"]
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_all_none_rows_do_not_count_as_scored(scratch_set, monkeypatch):
    """★ 分母数的是**真的算出了分的行**，不是 ragas 返回的行数。

    `ragas_runner.py` 的单题失败**不删行**：那一行保留、四项全 `None`
    （NaN→None，`raise_exceptions=False`），只有某项**整列**都没算出来才进 `errors`。
    拿 `len(rows)` 当分母就会把全 None 行算成「有分」—— 评审实测 506f109f：
    63 行里 19 行全 None，四项均值其实只算在 42–43 行上，63 就是那个被误当分母的数。
    """
    async def fake(batch, *a, **kw):
        # 第 1 题判官整题失败（全 None 行），第 2 题正常出分 —— 两行都返回
        rows = [{"faithfulness": None, "answer_relevancy": None,
                 "context_precision": None, "context_recall": None},
                {"faithfulness": 1.0, "answer_relevancy": 1.0,
                 "context_precision": 1.0, "context_recall": 1.0}]
        return {"available": True, "ok": True, "rows": rows,
                "means": {}, "errors": [], "ms": 0}

    monkeypatch.setattr(runner.ragas_service, "score_samples", fake)
    _graph_returning(monkeypatch, {"题一": _final(["d1"]), "题二": _final(["d1"])})
    async with db.tx() as conn:
        await _add_case(conn, scratch_set, "题一")
        await _add_case(conn, scratch_set, "题二")
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集")
    try:
        await runner.run_eval(run_id)

        metrics = (await _run_row(run_id))["metrics"]
        metrics = json.loads(metrics) if isinstance(metrics, str) else metrics
        assert metrics["ragas_scored_cases"] == 1, \
            "全 None 的行没算出分，不能进分母（63 vs 42–43 的那个错）"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)
