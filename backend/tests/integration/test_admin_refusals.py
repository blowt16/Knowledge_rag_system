"""管理端拒答明细与标注（§4.3.1.3）。

为什么明细是**独立接口**：`stats/refusals` 是**聚合**（饼图 / Top N 计数），
**不含可标注的记录标识** —— 要标注就得有 `qa_logs.id`。

⚠️ 标注落 `refusal_annotations`，且 `qa_log_id` 上有 **UNIQUE**：
   一条拒答记录**只保留最新一次标注**（标注是可反复修改的运维动作，
   不像日志那样只增）。第二次标注必须是 UPDATE，不是插第二行 ——
   插第二行会在 UNIQUE 上炸，或者（更糟）在没约束的实现里悄悄变成两条。
"""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio

from app import db
from app.core.security import hash_password
from app.main import app

PASSWORD = "Test@12345"


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _insert_user(role: str) -> dict:
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, $4, 0)",
            user_id, username, hash_password(PASSWORD), role,
        )
    return {"id": user_id, "username": username}


async def _login(client, username: str) -> str:
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin(client):
    u = await _insert_user("admin")
    u["token"] = await _login(client, u["username"])
    yield u
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


@pytest_asyncio.fixture
async def refusal_log(admin):
    """一条拒答的 qa_logs 行 + 一条**非**拒答的行（负例对照）。"""
    refused_id = uuid.uuid4().hex
    answered_id = uuid.uuid4().hex
    question = f"库里没有的问题_{uuid.uuid4().hex[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO qa_logs (id, session_id, user_id, user_role, question,
                                    is_refused, refusal_reason)
               VALUES ($1, 's1', $2, 'student', $3, 1, 'no_candidate')""",
            refused_id, admin["id"], question)
        await conn.execute(
            """INSERT INTO qa_logs (id, session_id, user_id, user_role, question,
                                    is_refused, refusal_reason, answer)
               VALUES ($1, 's2', $2, 'student', '库里有据的问题', 0, NULL, '答案')""",
            answered_id, admin["id"])
    try:
        yield {"refused_id": refused_id, "answered_id": answered_id,
               "question": question}
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM refusal_annotations WHERE qa_log_id = ANY($1)",
                               [refused_id, answered_id])
            await conn.execute("DELETE FROM qa_logs WHERE id = ANY($1)",
                               [refused_id, answered_id])


# ============================================================

async def test_list_refusals_returns_details_with_null_annotation(client, admin,
                                                                 refusal_log):
    r = await client.get("/api/admin/refusals", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"items", "total", "page", "page_size", "has_more"} <= set(body)

    mine = [i for i in body["items"] if i["id"] == refusal_log["refused_id"]]
    assert mine, "拒答记录必须出现在明细里"
    item = mine[0]
    for key in ("id", "question", "refusal_reason", "created_at", "annotation"):
        assert key in item, f"明细缺少 {key}（§4.3.1.3 的字段表）"
    assert item["question"] == refusal_log["question"]
    assert item["refusal_reason"] == "no_candidate"
    assert item["annotation"] is None, "还没标注时必须是 null，不是空对象"


async def test_list_refusals_excludes_answered(client, admin, refusal_log):
    """明细是「学生问了但答不上来」的清单 —— 答上来的不该混进去（§3.5）。"""
    body = (await client.get("/api/admin/refusals", headers=_h(admin["token"]))).json()
    assert refusal_log["answered_id"] not in {i["id"] for i in body["items"]}


async def test_annotate_with_note(client, admin, refusal_log):
    r = await client.post(f"/api/admin/refusals/{refusal_log['refused_id']}/annotate",
                          json={"note": "需要补一份《实验室安全管理办法》"},
                          headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    ann = r.json()["annotation"]
    assert ann["note"].startswith("需要补")
    assert ann["suggested_document_id"] is None
    assert ann["annotated_by"] == admin["id"], "要记下是谁标的"

    listed = (await client.get("/api/admin/refusals", headers=_h(admin["token"]))).json()
    item = next(i for i in listed["items"] if i["id"] == refusal_log["refused_id"])
    assert item["annotation"]["note"].startswith("需要补"), "标完要能在明细里看到"


async def test_annotate_with_suggested_document(client, admin, refusal_log):
    async with db.tx() as conn:
        doc_id = await conn.fetchval("SELECT id FROM documents LIMIT 1")
    if doc_id is None:
        import pytest
        pytest.skip("库里没有文档，无法验证 suggested_document_id")

    r = await client.post(f"/api/admin/refusals/{refusal_log['refused_id']}/annotate",
                          json={"suggested_document_id": doc_id},
                          headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["annotation"]["suggested_document_id"] == doc_id


async def test_annotate_with_no_fields_is_400(client, admin, refusal_log):
    """两个字段**至少填一个**，否则 400（§4.3.1.3）。"""
    r = await client.post(f"/api/admin/refusals/{refusal_log['refused_id']}/annotate",
                          json={}, headers=_h(admin["token"]))
    assert r.status_code == 400, r.text


async def test_annotate_twice_updates_not_inserts(client, admin, refusal_log):
    """★ 一条拒答记录**只保留最新一次标注**（`qa_log_id` 上有 UNIQUE）。"""
    log_id = refusal_log["refused_id"]
    await client.post(f"/api/admin/refusals/{log_id}/annotate",
                      json={"note": "第一次"}, headers=_h(admin["token"]))
    r = await client.post(f"/api/admin/refusals/{log_id}/annotate",
                          json={"note": "第二次"}, headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["annotation"]["note"] == "第二次"

    async with db.tx() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM refusal_annotations WHERE qa_log_id = $1", log_id)
    assert n == 1, "第二次标注插成了新行 —— 一条拒答只该有一条标注"


async def test_annotate_with_unknown_document_is_400(client, admin, refusal_log):
    """`suggested_document_id` 指向不存在的文档时必须拦下 ——
    不拦就会撞 `refusal_annotations` 的外键变成 500，管理员看不出是自己填错了。"""
    r = await client.post(f"/api/admin/refusals/{refusal_log['refused_id']}/annotate",
                          json={"suggested_document_id": uuid.uuid4().hex},
                          headers=_h(admin["token"]))
    assert r.status_code == 400, r.text


async def test_annotate_unknown_log_is_404(client, admin):
    r = await client.post(f"/api/admin/refusals/{uuid.uuid4().hex}/annotate",
                          json={"note": "x"}, headers=_h(admin["token"]))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


async def test_student_cannot_read_refusals(client, refusal_log):
    u = await _insert_user("student")
    token = await _login(client, u["username"])
    try:
        assert (await client.get("/api/admin/refusals",
                                 headers=_h(token))).status_code == 403
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", u["id"])
