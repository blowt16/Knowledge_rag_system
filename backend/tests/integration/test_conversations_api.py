"""会话列表与消息接口（§3.7.2）。

⚠️ **M4 之前这条链路是断的**：`conversation_service` 的
   `list_conversations` / `get_messages` 早就写好了，但**接口层根本没建** ——
   前端因此拿不到历史消息，刷新后引用角标无从渲染（§4.2.4）。

⚠️ **越权一律 404，不是 403**（M4-D4）：与 `/api/documents/{id}/file` 同口径 ——
   403 等于承认「这个 id 存在，只是不给你」，会话 id 是可枚举的，
   那就成了探测器。原实现是**静默返回 `[]`**，前端分不清
   「空会话」和「不是你的会话」。

⚠️ **软删除的会话既不出现在 items 里、也不计入 total**（§3.7.2）——
   只过滤 items 会让「共 N 条」永远比实际看到的多。
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


async def _make_user(client: httpx.AsyncClient) -> dict:
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, 'student', 0)",
            user_id, username, hash_password(PASSWORD),
        )
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"id": user_id, "username": username, "token": r.json()["access_token"]}


def _h(user: dict) -> dict:
    return {"Authorization": f"Bearer {user['token']}"}


@pytest_asyncio.fixture
async def users(client):
    """两个用户：`a` 是主人，`b` 用来验越权。"""
    a = await _make_user(client)
    b = await _make_user(client)
    yield a, b
    ids = [a["id"], b["id"]]
    async with db.tx() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE user_id = ANY($1))", ids)
        await conn.execute("DELETE FROM conversations WHERE user_id = ANY($1)", ids)
        await conn.execute("DELETE FROM users WHERE id = ANY($1)", ids)


async def _mk_conv(session_id: str, owner: str, *, title: str, last_chat: str = "now()",
                   is_top: int = 0, delete_flag: int = 0) -> None:
    async with db.tx() as conn:
        await conn.execute(
            f"""INSERT INTO conversations
                (id, user_id, title, is_top, delete_flag, last_chat_time)
                VALUES ($1, $2, $3, $4, $5, {last_chat})""",
            session_id, owner, title, is_top, delete_flag,
        )


# ============================================================
# 列表
# ============================================================

async def test_create_returns_item_and_appears_in_list(client, users):
    a, _ = users
    r = await client.post("/api/conversations", json={"title": "我的新会话"},
                          headers=_h(a))
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["id"] and created["title"] == "我的新会话"

    r = await client.get("/api/conversations", headers=_h(a))
    ids = [i["id"] for i in r.json()["items"]]
    assert created["id"] in ids


async def test_create_without_title_gets_default(client, users):
    a, _ = users
    r = await client.post("/api/conversations", json={}, headers=_h(a))
    assert r.status_code == 200, r.text
    assert r.json()["title"], "没给标题也要有个兜底标题，不能在列表里显示空白"


async def test_list_orders_by_top_then_last_chat_time(client, users):
    """排序固定 `is_top DESC, last_chat_time DESC`（§3.7.2）—— 不依赖插入顺序。"""
    a, _ = users
    older = uuid.uuid4().hex
    newer = uuid.uuid4().hex
    pinned = uuid.uuid4().hex
    await _mk_conv(older, a["id"], title="旧", last_chat="now() - interval '2 days'")
    await _mk_conv(newer, a["id"], title="新", last_chat="now() - interval '1 hour'")
    await _mk_conv(pinned, a["id"], title="置顶", last_chat="now() - interval '3 days'",
                   is_top=1)

    r = await client.get("/api/conversations", headers=_h(a))
    items = r.json()["items"]

    assert [i["title"] for i in items] == ["置顶", "新", "旧"], \
        "置顶优先，其余按最后对话时间倒序"


async def test_list_pagination_and_total(client, users):
    a, _ = users
    for i in range(3):
        await _mk_conv(uuid.uuid4().hex, a["id"], title=f"会话{i}",
                       last_chat=f"now() - interval '{i} minutes'")

    r = await client.get("/api/conversations?offset=0&limit=2", headers=_h(a))
    body = r.json()

    assert len(body["items"]) == 2
    assert body["total"] == 3, "total 是总数，不是本页条数"
    assert body["has_more"] is True, "还有一条 —— 前端要靠它决定滚到底要不要再拉"

    r = await client.get("/api/conversations?offset=2&limit=2", headers=_h(a))
    body = r.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is False


async def test_soft_deleted_excluded_from_items_and_total(client, users):
    a, _ = users
    keep = uuid.uuid4().hex
    gone = uuid.uuid4().hex
    await _mk_conv(keep, a["id"], title="留着")
    await _mk_conv(gone, a["id"], title="删了", delete_flag=1)

    body = (await client.get("/api/conversations", headers=_h(a))).json()

    assert [i["title"] for i in body["items"]] == ["留着"]
    assert body["total"] == 1, "软删的也不该计进 total —— 否则「共 N 条」永远对不上"


async def test_list_only_returns_own(client, users):
    a, b = users
    await _mk_conv(uuid.uuid4().hex, a["id"], title="A 的会话")

    body = (await client.get("/api/conversations", headers=_h(b))).json()

    assert body["items"] == [] and body["total"] == 0


async def test_list_requires_auth(client):
    r = await client.get("/api/conversations")
    assert r.status_code in (401, 403), r.text


# ============================================================
# 改名 / 置顶 / 删除
# ============================================================

async def test_patch_title(client, users):
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="旧标题")

    r = await client.patch(f"/api/conversations/{sid}", json={"title": "新标题"},
                           headers=_h(a))

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "新标题"
    body = (await client.get("/api/conversations", headers=_h(a))).json()
    assert body["items"][0]["title"] == "新标题"


async def test_patch_is_top_reorders_list(client, users):
    a, _ = users
    first = uuid.uuid4().hex
    second = uuid.uuid4().hex
    await _mk_conv(first, a["id"], title="A", last_chat="now() - interval '1 hour'")
    await _mk_conv(second, a["id"], title="B", last_chat="now() - interval '2 hours'")

    r = await client.patch(f"/api/conversations/{second}", json={"is_top": 1},
                           headers=_h(a))
    assert r.status_code == 200, r.text
    assert r.json()["is_top"] == 1

    titles = [i["title"] for i in
              (await client.get("/api/conversations", headers=_h(a))).json()["items"]]
    assert titles == ["B", "A"]


async def test_patch_with_no_fields_is_rejected(client, users):
    """两个字段至少给一个（§3.7.2）—— 空 PATCH 是个无意义请求，别静默放过。"""
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="标题")

    r = await client.patch(f"/api/conversations/{sid}", json={}, headers=_h(a))

    assert r.status_code == 400, r.text


async def test_patch_other_user_is_404_and_does_not_modify(client, users):
    a, b = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="A 的")

    r = await client.patch(f"/api/conversations/{sid}", json={"title": "被改了"},
                           headers=_h(b))

    assert r.status_code == 404, "越权必须是 404 —— 403 等于确认这个 id 存在"
    body = (await client.get("/api/conversations", headers=_h(a))).json()
    assert body["items"][0]["title"] == "A 的", "越权请求不得真的改到数据"


async def test_delete_soft_deletes_own(client, users):
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="待删")

    r = await client.delete(f"/api/conversations/{sid}", headers=_h(a))

    assert r.status_code == 200, r.text
    assert (await client.get("/api/conversations", headers=_h(a))).json()["items"] == []
    async with db.tx() as conn:
        flag = await conn.fetchval("SELECT delete_flag FROM conversations WHERE id = $1",
                                   sid)
    assert flag == 1, "必须是软删 —— 消息与 qa_logs 还要留着可查"


async def test_delete_other_user_is_404(client, users):
    a, b = users
    sid = uuid.uuid4().hex
    own = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="A 的")
    await _mk_conv(own, b["id"], title="B 自己的")

    # 对照：B 删自己的必须成功 —— 否则下面的 404 可能只是路由不存在
    assert (await client.delete(f"/api/conversations/{own}", headers=_h(b))).status_code == 200

    r = await client.delete(f"/api/conversations/{sid}", headers=_h(b))

    assert r.status_code == 404
    async with db.tx() as conn:
        flag = await conn.fetchval("SELECT delete_flag FROM conversations WHERE id = $1",
                                   sid)
    assert flag == 0, "越权删除不得生效"


# ============================================================
# 消息
# ============================================================

async def _mk_messages(sid: str) -> None:
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, route, created_at)
               VALUES ($1, $2, 'user', '问', 'knowledge', now())""",
            uuid.uuid4().hex, sid)
        # ⚠️ 传 **list**，不是 JSON 字符串：连接池给 jsonb 注册了编解码器
        #    （db.py 的 set_type_codec），再传字符串会被 json.dumps **二次编码**
        #    成一个 JSON 字符串标量 —— 读回来就是 str，不是 list。
        await conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, citations, created_at)
               VALUES ($1, $2, 'assistant', '答[1]', $3::jsonb, now() + interval '1 millisecond')""",
            uuid.uuid4().hex, sid,
            [{"marker": 1, "document_name": "文档", "chunk_id": "c1"}])


async def test_messages_ascending_with_citations(client, users):
    """消息按时间升序、带 `citations`（刷新后重新渲染引用角标的**唯一**来源）。"""
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="有消息")
    await _mk_messages(sid)

    r = await client.get(f"/api/conversations/{sid}/messages", headers=_h(a))

    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"], "必须按时间升序"
    assert msgs[1]["citations"][0]["marker"] == 1
    assert "created_at" in msgs[0] and "id" in msgs[0]


async def test_messages_of_other_user_is_404(client, users):
    a, b = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="A 的")
    await _mk_messages(sid)

    # 对照：主人的同一条 URL 必须是 200 —— 否则「404」可能只是因为路由压根不存在
    r_owner = await client.get(f"/api/conversations/{sid}/messages", headers=_h(a))
    assert r_owner.status_code == 200, r_owner.text

    r = await client.get(f"/api/conversations/{sid}/messages", headers=_h(b))

    assert r.status_code == 404, "越权读消息同样是 404，且不得返回内容"
    assert r.json()["code"] == "not_found", "必须是业务 404，不是「路由不存在」的 404"


async def test_messages_of_unknown_session_is_404(client, users):
    a, _ = users
    r = await client.get(f"/api/conversations/{uuid.uuid4().hex}/messages", headers=_h(a))
    assert r.status_code == 404, "不存在的会话与越权会话**不可区分**"
    assert r.json()["code"] == "not_found"


async def test_messages_of_soft_deleted_session_is_404(client, users):
    a, _ = users
    live = uuid.uuid4().hex
    gone = uuid.uuid4().hex
    await _mk_conv(live, a["id"], title="还在")
    await _mk_conv(gone, a["id"], title="删了", delete_flag=1)
    await _mk_messages(live)
    await _mk_messages(gone)

    r_live = await client.get(f"/api/conversations/{live}/messages", headers=_h(a))
    assert r_live.status_code == 200, "对照：没删的必须能读到"

    r = await client.get(f"/api/conversations/{gone}/messages", headers=_h(a))

    assert r.status_code == 404, "已删会话不该还能翻出消息"
