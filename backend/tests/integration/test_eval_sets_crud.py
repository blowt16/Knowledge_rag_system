"""评测集与用例的增删改查（§5.1 / §5.2）。

两条最容易做错、也最难事后发现的：

① **手工录入必须能插进去**。`case_type` 是 `NOT NULL` 且**没有默认值**，
   而参考图的「评测用例」弹窗只有 问题/标准答案/参与评测/备注 四个字段 ——
   服务端不自己填一个，第一次手工录入就拿到 `null value in column "case_type"`。

② **删用例不能动历史**。`eval_case_results.case_id` 改成 ON DELETE SET NULL 之后，
   删掉用例、那一轮的历史报告里那行**还在且内容完整**（快照列兜底），
   而不是 500、也不是整行消失。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app import db
from app.eval import sets


@pytest.fixture
async def scratch_set():
    set_id = uuid.uuid4().hex
    name = f"测试集-{set_id[:8]}"
    async with db.tx() as conn:
        await conn.execute("INSERT INTO eval_sets (id, name) VALUES ($1,$2)", set_id, name)
    yield {"id": set_id, "name": name}
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


# ============================================================
# 评测集
# ============================================================

async def test_create_list_rename_delete_set(scratch_set):
    async with db.tx() as conn:
        created = await sets.create_set(conn, "售后问题测评", "说明文字")
        assert created["name"] == "售后问题测评"
        assert created["description"] == "说明文字"

        listed = {s["id"]: s for s in await sets.list_sets(conn)}
        assert created["id"] in listed

        renamed = await sets.update_set(conn, created["id"], name="售后问题测评v2")
        assert renamed["name"] == "售后问题测评v2"
        # 只改名时说明不动
        assert renamed["description"] == "说明文字"

        await sets.delete_set(conn, created["id"])
        assert created["id"] not in {s["id"] for s in await sets.list_sets(conn)}


async def test_duplicate_set_name_raises_a_typed_error(scratch_set):
    """重名要走 409，不是把数据库的 IntegrityError 冒到接口层变成 500。"""
    async with db.tx() as conn:
        with pytest.raises(sets.SetNameTaken):
            await sets.create_set(conn, scratch_set["name"])
        with pytest.raises(sets.SetNameTaken):
            await sets.update_set(conn, scratch_set["id"], name="默认题库")


async def test_set_list_carries_the_case_count(scratch_set):
    """界面下拉里的「（N 条用例）」= 该集**全部**用例数，不是只数 in_eval（§11.3-4）。"""
    async with db.tx() as conn:
        await sets.create_case(conn, scratch_set["id"], question="q1", ground_truth="a1")
        await sets.create_case(conn, scratch_set["id"], question="q2", ground_truth="a2",
                               in_eval=False)
        row = next(s for s in await sets.list_sets(conn) if s["id"] == scratch_set["id"])
    assert row["case_count"] == 2


async def test_deleting_a_set_takes_its_cases_but_not_the_history(scratch_set):
    """决策 14：删集**连带删它的用例**；历史 run 不受影响（`set_name` 是快照）。"""
    run_id = uuid.uuid4().hex
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"],
                                      question="会被删掉的问题", ground_truth="答案")
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, status, set_id, set_name, metrics)
               VALUES ($1,'历史轮次','{}'::jsonb,'done',$2,$3,'{}'::jsonb)""",
            run_id, scratch_set["id"], scratch_set["name"])
        await conn.execute(
            """INSERT INTO eval_case_results (id, run_id, case_id, question, ground_truth,
                                             answer, metrics)
               VALUES ($1,$2,$3,$4,$5,$6,'{}'::jsonb)""",
            uuid.uuid4().hex, run_id, case["id"], "会被删掉的问题", "答案", "生成的答案")
    try:
        async with db.tx() as conn:
            await sets.delete_set(conn, scratch_set["id"])
            assert await conn.fetchval(
                "SELECT count(*) FROM eval_cases WHERE set_id = $1",
                scratch_set["id"]) == 0
            hist = await conn.fetchrow(
                "SELECT set_id, set_name FROM eval_runs WHERE id = $1", run_id)
            # 快照还在，只是不再指向那个集
            assert hist["set_name"] == scratch_set["name"]
            assert hist["set_id"] is None
            row = await conn.fetchrow(
                "SELECT case_id, question, ground_truth FROM eval_case_results "
                "WHERE run_id = $1", run_id)
            assert row["case_id"] is None, "用例删了，历史行的 case_id 应该置空"
            assert row["question"] == "会被删掉的问题" and row["ground_truth"] == "答案"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id = $1", run_id)


# ============================================================
# 用例
# ============================================================

async def test_manual_case_gets_the_fields_the_dialog_does_not_ask_for(scratch_set):
    """手工录入：弹窗没有的字段由服务端填好 —— 否则 `case_type` 非空约束当场报错。"""
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"],
                                      question="缓考要提前跟谁说？", ground_truth="所在学院")
        row = dict(await conn.fetchrow("SELECT * FROM eval_cases WHERE id=$1", case["id"]))
    assert row["case_type"] == "factual"
    assert row["suite"] == "full"
    assert row["source"] == "manual"
    assert row["set_id"] == scratch_set["id"]
    assert row["in_eval"] is True
    await_expected = json.loads(row["expected_doc_ids"]) if row["expected_doc_ids"] else None
    assert await_expected is None, "弹窗没这一项，就该留空（§5.2）"


async def test_case_filters_search_and_paging(scratch_set):
    async with db.tx() as conn:
        for i in range(5):
            await sets.create_case(conn, scratch_set["id"],
                                   question=f"缓考问题{i}", ground_truth=f"答案{i}")
        gen = await sets.create_case(conn, scratch_set["id"],
                                     question="自动生成的题", ground_truth="答案")
        await conn.execute("UPDATE eval_cases SET source='generated' WHERE id=$1", gen["id"])

        by_source = await sets.list_cases(conn, scratch_set["id"], source="generated")
        assert [c["id"] for c in by_source["items"]] == [gen["id"]]
        assert by_source["total"] == 1

        by_q = await sets.list_cases(conn, scratch_set["id"], q="自动生成")
        assert [c["id"] for c in by_q["items"]] == [gen["id"]]

        page1 = await sets.list_cases(conn, scratch_set["id"], page=1, page_size=2)
        page2 = await sets.list_cases(conn, scratch_set["id"], page=2, page_size=2)
        assert page1["total"] == 6
        assert len(page1["items"]) == 2 and len(page2["items"]) == 2
        assert {c["id"] for c in page1["items"]} & {c["id"] for c in page2["items"]} == set()

        # 翻过头 → 空列表，不是报错
        far = await sets.list_cases(conn, scratch_set["id"], page=99, page_size=2)
        assert far["items"] == [] and far["total"] == 6


async def test_case_paging_is_clamped(scratch_set):
    """全站 `page_size ≤ 100`（§4.3.1.1）；越界要夹住，不是原样传下去。"""
    async with db.tx() as conn:
        assert (await sets.list_cases(conn, scratch_set["id"], page=0, page_size=999))["page"] == 1
        got = await sets.list_cases(conn, scratch_set["id"], page_size=999)
        assert got["page_size"] == 100


async def test_patch_and_delete_case(scratch_set):
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"],
                                      question="原问题", ground_truth="原答案")
        patched = await sets.update_case(conn, case["id"], question="改后的问题",
                                         in_eval=False, note="备注")
        assert patched["question"] == "改后的问题"
        assert patched["in_eval"] is False
        assert patched["note"] == "备注"
        # 没传的列不动
        assert patched["ground_truth"] == "原答案"

        await sets.delete_case(conn, case["id"])
        assert await conn.fetchval("SELECT count(*) FROM eval_cases WHERE id=$1",
                                   case["id"]) == 0


async def test_update_case_ignores_columns_that_are_not_editable(scratch_set):
    """`source` / `set_id` / `source_*` 改了就没有「来源」可言了（§5.2）。"""
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"], question="q", ground_truth="a")
        await conn.execute("UPDATE eval_cases SET source_document_id=NULL, source_chunk_id=$2,"
                           " source_page=3, source_snippet=$3, source='generated' WHERE id=$1",
                           case["id"], "chunk-1", "原文")
        patched = await sets.update_case(conn, case["id"], question="q2",
                                         source="manual", source_page=99,
                                         source_snippet="改过的")
    assert patched["source"] == "generated"
    assert patched["source_page"] == 3
    assert patched["source_snippet"] == "原文"


async def test_update_missing_case_raises(scratch_set):
    async with db.tx() as conn:
        with pytest.raises(sets.CaseNotFound):
            await sets.update_case(conn, "no-such-case", question="x")
        with pytest.raises(sets.CaseNotFound):
            await sets.delete_case(conn, "no-such-case")


# ============================================================
# 来源片段与高亮（§5.2 / §6.5）
# ============================================================

def test_highlight_survives_pdf_hard_wraps():
    """⚠️ **不能直接 `snippet.find(ground_truth)`。**

    规范化正文里有 PDF 提取留下的硬换行（`…提出申请并经\\n\\n学院审核同意后送达；`），
    模型复述时自然写成一行 —— 用带空白的原串去比会判成「不是原文」，而**它确实是原文**。
    所以按**去空白口径**定位，再把区间映射回原文偏移。
    """
    snippet = "第七条 缓考\n\n学生因病因事不能参加考试的，\n应当在考试前向所在学院提出申请，\n\n经批准后方可缓考。"
    answer = "学生因病因事不能参加考试的，应当在考试前向所在学院提出申请，经批准后方可缓考。"

    span = sets.locate_highlight(snippet, answer)
    assert span is not None, "去空白口径下定位失败"
    start, end = span
    assert snippet[start:end] == "学生因病因事不能参加考试的，\n应当在考试前向所在学院提出申请，\n\n经批准后方可缓考。"
    # 高亮区间内的**非空白**字符必须与原答案逐字相同
    assert "".join(snippet[start:end].split()) == answer


def test_highlight_returns_none_when_it_does_not_line_up():
    """对不上就返回 null —— 这往往是**文档更新过了**，正是该功能要暴露的（§8.3）。"""
    assert sets.locate_highlight("完全不同的一段原文", "标准答案") is None
    assert sets.locate_highlight("", "标准答案") is None
    assert sets.locate_highlight("原文", "") is None
    # 只差标点也算对不上（去空白不是去标点）
    assert sets.locate_highlight("学生应当在考试前提出申请", "学生应当在考试前提出申请。") is None


async def test_case_source_payload(scratch_set):
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"], question="q", ground_truth="3 个月")
        await conn.execute(
            "UPDATE eval_cases SET source_page=3, source_chunk_id='chunk-237',"
            " source_snippet=$2, source='generated' WHERE id=$1",
            case["id"], "二、有偿维修的部件自维修完成之日起保修 3 个月。")
        payload = await sets.case_source(conn, case["id"])
    assert payload["question"] == "q"
    assert payload["ground_truth"] == "3 个月"
    assert payload["source_page"] == 3
    assert payload["source_chunk_id"] == "chunk-237"
    start, end = payload["highlight"]
    assert payload["source_snippet"][start:end] == "3 个月"


# ============================================================
# 跑测期间的删除守卫（§5.5）
# ============================================================

async def test_delete_is_blocked_while_a_run_is_using_the_set(scratch_set):
    run_id = uuid.uuid4().hex
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"], question="q", ground_truth="a")
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, status, set_id, set_name)
               VALUES ($1,'跑着的轮次','{}'::jsonb,'running',$2,$3)""",
            run_id, scratch_set["id"], scratch_set["name"])
    try:
        async with db.tx() as conn:
            with pytest.raises(sets.SetBusy):
                await sets.delete_set(conn, scratch_set["id"])
            with pytest.raises(sets.SetBusy):
                await sets.delete_case(conn, case["id"])
        # ⚠️ 删**别的**评测集不受影响
        async with db.tx() as conn:
            other = await sets.create_set(conn, "另一个集")
            await sets.delete_set(conn, other["id"])
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id = $1", run_id)


async def test_delete_is_allowed_once_the_run_is_finished(scratch_set):
    run_id = uuid.uuid4().hex
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"], question="q", ground_truth="a")
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, status, set_id, set_name)
               VALUES ($1,'跑完的轮次','{}'::jsonb,'done',$2,$3)""",
            run_id, scratch_set["id"], scratch_set["name"])
    try:
        async with db.tx() as conn:
            await sets.delete_case(conn, case["id"])
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id = $1", run_id)
