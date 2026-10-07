"""评测接口（§5.1–§5.4）—— 全 admin。

⚠️ 这里盯的三处，错了都不会报错、只会**悄悄给错东西**：

① **`POST /run` 的两条路径**（§5.4）。`config` 是否非空决定这一轮是「线上链路」
   还是「消融运行」—— 而 `api/eval.py` 原来**无条件**往 config 里塞 `suite`。
   新页面走 `set_id`，如果照旧塞，`switches()` 就返回非 None →
   **点「开始评测」跑出来的是纯向量基线**，而界面写着「线上链路」。

② **`config_label` 两处必须一致**。创建响应原来读 `payload.config`（请求体里的），
   列表读库里的 `cfg`。消融页点「跑全量」时前者是 `None` → 显示「线上链路」，
   实际跑的是纯向量基线，列表里也写着「纯向量检索」—— **创建的那一刻就在骗人**。

③ **schema 漏字段**。`GET /runs/{id}` 用 `response_model=EvalRunDetail`，
   FastAPI 会**静默丢弃**响应模型里没声明的字段 —— 不报错、不警告，
   前端就是收不到，整个报告弹窗是空的而接口看着完全正常。
"""

from __future__ import annotations

import json
import uuid

import pytest_asyncio
import httpx

from app import db
from app.core.security import hash_password
from app.main import app

PASSWORD = "Test@12345"


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin(client):
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, 'admin', 0)",
            user_id, username, hash_password(PASSWORD))
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    yield {"id": user_id, "token": r.json()["access_token"], "headers": {
        "Authorization": f"Bearer {r.json()['access_token']}"}}
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def _cleanup_set(set_id: str) -> None:
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_runs WHERE set_id = $1", set_id)
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


async def _new_set(client, admin, name=None) -> dict:
    name = name or f"接口测试集-{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/admin/eval/sets", json={"name": name, "description": "说明"},
                          headers=admin["headers"])
    assert r.status_code == 201, r.text
    return r.json()


# ============================================================
# 评测集（§5.1）
# ============================================================

async def test_set_crud_over_http(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.get("/api/admin/eval/sets", headers=admin["headers"])
        assert r.status_code == 200
        mine = next(x for x in r.json()["items"] if x["id"] == s["id"])
        assert mine["case_count"] == 0 and mine["name"] == s["name"]

        r = await client.patch(f"/api/admin/eval/sets/{s['id']}",
                               json={"name": s["name"] + "改"}, headers=admin["headers"])
        assert r.status_code == 200 and r.json()["name"].endswith("改")

        r = await client.delete(f"/api/admin/eval/sets/{s['id']}", headers=admin["headers"])
        assert r.status_code == 204
    finally:
        await _cleanup_set(s["id"])


async def test_duplicate_name_is_409_not_500(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.post("/api/admin/eval/sets", json={"name": s["name"]},
                              headers=admin["headers"])
        assert r.status_code == 409, r.text
        assert "同名" in r.json()["message"]

        r = await client.patch("/api/admin/eval/sets/默认题库",
                               json={}, headers=admin["headers"])
        r2 = await client.patch(f"/api/admin/eval/sets/{s['id']}",
                                json={"name": "默认题库"}, headers=admin["headers"])
        assert r2.status_code == 409, r2.text
    finally:
        await _cleanup_set(s["id"])


async def test_missing_set_is_404(client, admin):
    for method, path, body in (
            ("get", "/api/admin/eval/sets/nope/export", None),
            ("patch", "/api/admin/eval/sets/nope", {"name": "x"}),
            ("delete", "/api/admin/eval/sets/nope", None),
    ):
        r = await getattr(client, method)(path, headers=admin["headers"],
                                          **({"json": body} if body else {}))
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"


async def test_export_returns_a_downloadable_json(client, admin):
    s = await _new_set(client, admin, name="导出用/带斜杠")
    try:
        await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                          json={"question": "问题", "ground_truth": "答案"},
                          headers=admin["headers"])
        r = await client.get(f"/api/admin/eval/sets/{s['id']}/export",
                             headers=admin["headers"])
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        disp = r.headers["content-disposition"]
        # RFC 5987 形式，中文名不乱码；且**不能**有裸的换行/引号（会让头断行）
        assert "filename*=UTF-8''" in disp
        assert "\n" not in disp and '"' not in disp.split("filename*=")[1]
        assert "%2F" in disp or "%E5%AF%BC%E5%87%BA" in disp
        payload = json.loads(r.content.decode("utf-8"))
        assert payload["set"]["name"] == "导出用/带斜杠"
        assert len(payload["cases"]) == 1
    finally:
        await _cleanup_set(s["id"])


async def test_empty_set_exports_with_no_cases(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.get(f"/api/admin/eval/sets/{s['id']}/export",
                             headers=admin["headers"])
        assert r.status_code == 200
        assert json.loads(r.content.decode("utf-8"))["cases"] == []
    finally:
        await _cleanup_set(s["id"])


# ============================================================
# 用例（§5.2）
# ============================================================

async def test_manual_case_round_trip(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                              json={"question": "缓考要跟谁说？", "ground_truth": "所在学院"},
                              headers=admin["headers"])
        assert r.status_code == 201, r.text
        case = r.json()
        assert case["case_type"] == "factual" and case["source"] == "manual"

        r = await client.patch(f"/api/admin/eval/cases/{case['id']}",
                               json={"in_eval": False, "note": "备注"},
                               headers=admin["headers"])
        assert r.status_code == 200 and r.json()["in_eval"] is False

        r = await client.delete(f"/api/admin/eval/cases/{case['id']}",
                                headers=admin["headers"])
        assert r.status_code == 204
    finally:
        await _cleanup_set(s["id"])


async def test_case_list_filters_and_pages(client, admin):
    s = await _new_set(client, admin)
    try:
        for i in range(3):
            await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                              json={"question": f"缓考问题{i}", "ground_truth": "答"},
                              headers=admin["headers"])
        r = await client.get(f"/api/admin/eval/sets/{s['id']}/cases",
                             params={"q": "问题1", "page": 1, "page_size": 10},
                             headers=admin["headers"])
        body = r.json()
        assert body["total"] == 1 and body["items"][0]["question"] == "缓考问题1"

        r = await client.get(f"/api/admin/eval/sets/{s['id']}/cases",
                             params={"source": "generated"}, headers=admin["headers"])
        assert r.json()["total"] == 0

        # 全站 page_size ≤ 100（§4.3.1.1）—— 与 admin.py 的列表接口同一写法：
        # 越界是 **422**，不是悄悄夹住（夹住会让调用方以为拿到了 999 条）
        r = await client.get(f"/api/admin/eval/sets/{s['id']}/cases",
                             params={"page_size": 999}, headers=admin["headers"])
        assert r.status_code == 422
    finally:
        await _cleanup_set(s["id"])


async def test_case_source_endpoint(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                              json={"question": "问题", "ground_truth": "3 个月"},
                              headers=admin["headers"])
        case_id = r.json()["id"]
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE eval_cases SET source='generated', source_page=3,"
                " source_snippet='二、有偿维修的部件自维修完成之日起保修 3 个月。'"
                " WHERE id=$1", case_id)
        r = await client.get(f"/api/admin/eval/cases/{case_id}/source",
                             headers=admin["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["source_page"] == 3
        start, end = body["highlight"]
        assert body["source_snippet"][start:end] == "3 个月"

        r = await client.get("/api/admin/eval/cases/nope/source", headers=admin["headers"])
        assert r.status_code == 404
    finally:
        await _cleanup_set(s["id"])


# ============================================================
# 建 run 的两条路径（§5.4）
# ============================================================

async def _run_row(run_id: str) -> dict:
    async with db.tx() as conn:
        return dict(await conn.fetchrow("SELECT * FROM eval_runs WHERE id=$1", run_id))


async def test_set_path_leaves_config_empty(client, admin):
    """`set_id` 路径的 `config` 落库必须是 `{}` —— 非空就会被判成消融运行、
    又跑成纯向量基线，而界面写着「线上链路」。"""
    s = await _new_set(client, admin)
    run_id = None
    try:
        await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                          json={"question": "q", "ground_truth": "a"},
                          headers=admin["headers"])
        r = await client.post("/api/admin/eval/run",
                              json={"set_id": s["id"], "role": "student"},
                              headers=admin["headers"])
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        row = await _run_row(run_id)
        cfg = row["config"]
        cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
        assert cfg == {}, f"set_id 路径的 config 必须为空，实际 {cfg}"
        assert row["set_id"] == s["id"]
        assert row["set_name"] == s["name"], "set_name 是快照，建 run 时就该写上"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE set_id=$1", s["id"])
        await _cleanup_set(s["id"])


async def test_suite_path_keeps_the_suite_key(client, admin):
    """老路径照旧：`config` 里带 `suite` 键 —— 这正是它被判成消融运行的原因。"""
    r = await client.post("/api/admin/eval/run",
                          json={"suite": "refusal_calib", "role": "student"},
                          headers=admin["headers"])
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    try:
        row = await _run_row(run_id)
        cfg = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
        assert cfg.get("suite") == "refusal_calib"
        assert row["set_id"] is None
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_config_label_matches_between_create_and_list(client, admin):
    """⚠️ 创建响应的 label 与列表里的 label **必须一模一样**。

    原来创建那处读的是请求体里的 `payload.config`（消融页传 `None`），
    列表那处读库里的 `cfg` —— 同一个 run 两个名字，而且创建时显示的是
    「线上链路」、实际跑的是纯向量基线。
    """
    r = await client.post("/api/admin/eval/run",
                          json={"suite": "full", "role": "student",
                                "config": {"bm25": True}},
                          headers=admin["headers"])
    assert r.status_code == 202, r.text
    created = r.json()
    run_id = created["run_id"]
    try:
        listed = await client.get("/api/admin/eval/runs", headers=admin["headers"])
        row = next(x for x in listed.json()["items"] if x["run_id"] == run_id)
        assert created["config_label"] == row["config_label"] == "开启 BM25"

        # 不带 config 的消融轮：两边都得是「纯向量检索」，不能是「线上链路」
        r2 = await client.post("/api/admin/eval/run",
                               json={"suite": "full", "role": "student"},
                               headers=admin["headers"])
        # 已有轮在跑 → 409，属正常（一次只允许一轮）
        if r2.status_code == 202:
            rid2 = r2.json()["run_id"]
            listed2 = await client.get("/api/admin/eval/runs", headers=admin["headers"])
            row2 = next(x for x in listed2.json()["items"] if x["run_id"] == rid2)
            assert r2.json()["config_label"] == row2["config_label"] == "纯向量检索"
            async with db.tx() as conn:
                await conn.execute("DELETE FROM eval_runs WHERE id=$1", rid2)
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_run_on_missing_set_is_404(client, admin):
    r = await client.post("/api/admin/eval/run",
                          json={"set_id": "nope", "role": "student"},
                          headers=admin["headers"])
    assert r.status_code == 404, r.text


async def test_runs_list_is_paginated(client, admin):
    r = await client.get("/api/admin/eval/runs",
                         params={"page": 1, "page_size": 2}, headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert len(body["items"]) <= 2

    r = await client.get("/api/admin/eval/runs",
                         params={"page_size": 999}, headers=admin["headers"])
    assert r.status_code == 422, "全站 page_size ≤ 100（§4.3.1.1）"
    r = await client.get("/api/admin/eval/runs", params={"page_size": 100},
                         headers=admin["headers"])
    assert r.json()["page_size"] == 100


# ============================================================
# 删 run / 看报告（§5.3 / §7.1）
# ============================================================

async def _make_finished_run(metrics: dict | None = None) -> str:
    """造一轮跑完的 run + 逐题结果，用来验报告与删除。"""
    run_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted, status,
                                      metrics, started_at, finished_at, total_cases, done_cases)
               VALUES ($1,'报告用','{}'::jsonb,'student',0,'done',$2::jsonb,
                       now() - interval '30 seconds', now(), 1, 1)""",
            run_id, json.dumps(metrics or {}))
        await conn.execute(
            """INSERT INTO eval_case_results
                 (id, run_id, case_id, question, ground_truth, answer, metrics, unauthorized_hits)
               VALUES ($1,$2,NULL,'问题','标准答案','生成的答案',$3::jsonb,0)""",
            uuid.uuid4().hex, run_id,
            json.dumps({"ragas_context_recall": 1.0, "ragas_context_precision": 0.7,
                        "ragas_faithfulness": 1.0, "ragas_answer_relevancy": 0.9951}))
    return run_id


async def test_report_block_is_served_with_the_run(client, admin):
    run_id = await _make_finished_run({
        "ragas": {"context_recall": 1.0, "context_precision": 0.7,
                  "faithfulness": 1.0, "answer_relevancy": 0.9951},
        "ragas_available": True, "ragas_errors": []})
    try:
        r = await client.get(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report"]["composite_score"] == 0.9238
        assert [c["key"] for c in body["report"]["metrics"]] == [
            "context_recall", "context_precision", "faithfulness", "answer_relevancy"]
        assert body["cases"][0]["question"] == "问题"
        assert body["cases"][0]["score"] == 0.9238
        # 耗时现算，不存列
        assert body["duration_ms"] == 30000
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_run_detail_survives_a_deleted_case(client, admin):
    """删用例不动历史：`case_id` 为空的历史报告**照样打得开**，不是 500。

    （`EvalCaseResult.case_id` 原来是必填 str —— 库里的值一旦是 NULL，
      `GET /runs/{id}` 会因响应校验失败 500。）
    """
    run_id = await _make_finished_run({"ragas_available": True})
    try:
        r = await client.get(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["cases"][0]["case_id"] is None
        assert r.json()["cases"][0]["question"] == "问题"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_delete_run_takes_its_case_results(client, admin):
    """删一轮 = 连它的逐题结果一起删（靠 ON DELETE CASCADE）。"""
    run_id = await _make_finished_run({"ragas_available": True})
    r = await client.delete(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
    assert r.status_code == 204, r.text
    async with db.tx() as conn:
        assert await conn.fetchval("SELECT count(*) FROM eval_runs WHERE id=$1", run_id) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM eval_case_results WHERE run_id=$1", run_id) == 0

    r = await client.delete(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
    assert r.status_code == 404


async def test_run_list_carries_the_new_columns(client, admin):
    """列表要带 set_name / 进度 / 失败原因 / 耗时 —— 任务表那几列吃这些。"""
    run_id = await _make_finished_run({"ragas_available": True})
    try:
        r = await client.get("/api/admin/eval/runs", headers=admin["headers"])
        row = next(x for x in r.json()["items"] if x["run_id"] == run_id)
        for key in ("set_name", "done_cases", "total_cases", "error", "duration_ms"):
            assert key in row, f"列表少了 {key}（schema 漏字段会被静默丢弃）"
        assert row["total_cases"] == 1 and row["done_cases"] == 1
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


# ============================================================
# 删除守卫（§5.5）
# ============================================================

async def test_deleting_a_set_under_a_running_run_is_409(client, admin):
    s = await _new_set(client, admin)
    run_id = uuid.uuid4().hex
    try:
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO eval_runs (id, name, config, role, include_restricted,
                                          status, set_id, set_name)
                   VALUES ($1,'跑着的','{}'::jsonb,'student',0,'running',$2,$3)""",
                run_id, s["id"], s["name"])
        r = await client.delete(f"/api/admin/eval/sets/{s['id']}", headers=admin["headers"])
        assert r.status_code == 409, r.text
        assert "正在被评测使用" in r.json()["message"]
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)
        await _cleanup_set(s["id"])


async def test_deleting_a_running_run_is_refused(client, admin):
    """⚠️ **不许删正在跑的轮次**（评审发现）。

    删行**不会停掉后台那条 LangGraph 任务** —— 它还在占 GPU。行没了之后
    「已有评测在跑」的检查就查不到任何东西，用户可以立刻再起一轮，
    两轮同时跑，正是注释里写的「两轮并跑既慢又会把显存挤爆」。
    上一轮最后会静默失败（更新 0 行、批量 INSERT 外键违约），用户什么都看不到。
    """
    run_id = uuid.uuid4().hex
    try:
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO eval_runs (id, name, config, role, include_restricted, status)
                   VALUES ($1,'跑着的','{}'::jsonb,'student',0,'running')""", run_id)
        r = await client.delete(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
        assert r.status_code == 409, r.text
        assert "等它结束" in r.json()["message"]
        async with db.tx() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM eval_runs WHERE id=$1", run_id) == 1, "行没被删"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_deleting_a_finished_run_is_still_allowed(client, admin):
    run_id = await _make_finished_run({"ragas_available": True})
    r = await client.delete(f"/api/admin/eval/runs/{run_id}", headers=admin["headers"])
    assert r.status_code == 204, r.text
