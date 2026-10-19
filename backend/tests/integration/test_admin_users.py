"""管理端用户与角色（§3.7.3 / §4.3 用户管理 / §3.7.1）。

⚠️ **三处写操作都要 `token_version += 1`**（§3.7.3 的接口清单末尾）：
   建号 / 改角色 / 重置口令。自增即让该用户**所有已签发令牌立即失效** ——
   这正是「改了角色但旧令牌还能用」这种漏洞的解药。
   本文件里最要紧的两条就是：**改角色后旧 access 立刻 401**、
   **重置口令后旧 access 立刻 401**。只断言「返回 200」是测不出这个的。

⚠️ **停用**（§4.3 用户管理的「改角色 / 停用」）在库里原本没有落点 ——
   `users` 表没有可用性字段。本任务加 `is_active`（002 迁移），
   并且**在校验口令之后**才判停用：口令错的人拿到的仍是同一条
   「用户名或口令不正确」，不泄露账号是否存在。

⚠️ **删除用户**（§4.3 用户管理）是这里**唯一不自增 `token_version` 的写操作**：
   行都没了，自增无处可增，令牌靠 `deps.current_user` 查库查不到来作废。
   别照抄上面那条「三处写操作都要自增」的规矩往上套。
"""

from __future__ import annotations

import logging
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


async def _insert_user(role: str, password: str = PASSWORD) -> dict:
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, $4, 0)",
            user_id, username, hash_password(password), role,
        )
    return {"id": user_id, "username": username}


async def _login(client: httpx.AsyncClient, username: str,
                 password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/auth/login",
                            json={"username": username, "password": password})


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_conversation(user_id: str, *, messages: int = 2) -> str:
    """给用户造一个会话 + 若干消息，返回会话 id。

    删除用户绕不开这一层：`conversations.user_id` 是 **NOT NULL 外键**，
    没有历史的空壳账号怎么删都成，有历史才是真难点。
    """
    conv_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO conversations (id, user_id, title) VALUES ($1, $2, '测试会话')",
            conv_id, user_id)
        for i in range(messages):
            await conn.execute(
                """INSERT INTO messages (id, conversation_id, role, content)
                   VALUES ($1, $2, $3, $4)""",
                uuid.uuid4().hex, conv_id,
                "user" if i % 2 == 0 else "assistant", f"第 {i + 1} 条")
    return conv_id


async def _seed_document(uploader_id: str) -> str:
    """直接插一行 `documents`，**不走 `ingest()`**。

    删用户要验的是外键归属，不是入库流程。走真入库会往真实 Chroma /
    BM25 里写字，而测试清理那套的坑见 `tests/support.py` 顶部。

    ⚠️ 状态用 `disabled` 而**不是** `active`：`active` 意味着正文已落盘
       （`normalized_text_path` 非空），而这里只插一行、那个字段是 NULL。
       插成 active 会凭空多出一个「没有正文的生效文档」——
       `test_eval_cases` / `test_eval_fixture` / `test_eval_generate` /
       `test_admin_stats` 都会去扫 `status='active'`，一旦本文件清理失败
       （RED 阶段就会：事务回滚会把整批清理一起撤掉），孤儿行会让**那几个
       毫不相干的用例**报出 `Path(None)` 这种看不懂的错。实测踩过一次。
    """
    doc_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO documents
               (id, doc_group_id, title, filename, file_type, md5, version,
                effective_date, status, visibility, uploader_id)
               VALUES ($1, $1, $2, 'x.txt', 'txt', $3, 1, CURRENT_DATE,
                       'disabled', 'public', $4)""",
            doc_id, f"{doc_id}.txt", uuid.uuid4().hex, uploader_id)
    return doc_id


async def _seed_eval_set(created_by: str) -> str:
    set_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO eval_sets (id, name, created_by) VALUES ($1, $2, $3)",
            set_id, f"t_{set_id[:8]}", created_by)
    return set_id


async def _seed_qa_log(user_id: str) -> str:
    """qa_logs 无外键、按设计**只增不删**（管理端拒答分析的数据源）。"""
    log_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO qa_logs (id, user_id, user_role, question) "
            "VALUES ($1, $2, 'student', '测试提问')",
            log_id, user_id)
    return log_id


@pytest_asyncio.fixture
async def admin(client):
    u = await _insert_user("admin")
    r = await _login(client, u["username"])
    u["token"] = r.json()["access_token"]
    yield u
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


@pytest_asyncio.fixture
async def victim(client):
    """被管理的普通用户 —— 用它的旧令牌验证「撤销真的生效」。"""
    u = await _insert_user("student")
    u["token"] = (await _login(client, u["username"])).json()["access_token"]
    yield u
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


@pytest_asyncio.fixture
async def target(victim):
    """一个「有历史」的被删对象：会话+消息、传过的文档、建的评测集、qa_logs。

    删除接口要清的（会话、消息、users 行）由它自己清；这里只清
    **删接口特意保留**的那几行。victim 的 teardown 兜最后的 users 行 ——
    它依赖本夹具，收尾顺序保证在本夹具之后跑。
    """
    victim["conversation_id"] = await _seed_conversation(victim["id"])
    victim["document_id"] = await _seed_document(victim["id"])
    victim["eval_set_id"] = await _seed_eval_set(victim["id"])
    victim["qa_log_id"] = await _seed_qa_log(victim["id"])
    yield victim
    async with db.tx() as conn:
        # ⚠️ messages 必须先于 conversations：外键 NOT NULL，顺序反了这整段事务
        #    会回滚，前面几条删除跟着一起白删，然后 victim 的 teardown 撞
        #    documents 外键报错 —— 报错点离真正的错因很远，实测踩过。
        await conn.execute(
            """DELETE FROM messages WHERE conversation_id IN
                   (SELECT id FROM conversations WHERE user_id = $1)""", victim["id"])
        await conn.execute("DELETE FROM conversations WHERE user_id = $1", victim["id"])
        await conn.execute("DELETE FROM documents WHERE id = $1", victim["document_id"])
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", victim["eval_set_id"])
        await conn.execute("DELETE FROM qa_logs WHERE id = $1", victim["qa_log_id"])


# ============================================================
# 角色清单
# ============================================================

async def test_roles_returns_value_and_label(client, admin):
    """可见范围选择器要用它 —— **不许前端硬编码**（§4.3.1.2）。"""
    r = await client.get("/api/admin/roles", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    roles = r.json()

    values = {x["value"] for x in roles}
    assert {"student", "staff", "admin"} <= values
    for x in roles:
        assert x["label"].strip(), f"{x['value']} 没有中文标签"


async def test_student_cannot_access_admin_users(client):
    u = await _insert_user("student")
    token = (await _login(client, u["username"])).json()["access_token"]
    try:
        r = await client.get("/api/admin/users", headers=_h(token))
        assert r.status_code == 403, r.text
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", u["id"])


# ============================================================
# 列表与建号
# ============================================================

async def test_list_users_and_filter_by_role(client, admin, victim):
    r = await client.get("/api/admin/users", headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"items", "total", "page", "page_size", "has_more"} <= set(body)
    assert victim["id"] in {i["id"] for i in body["items"]}

    only_admin = (await client.get("/api/admin/users?role=admin",
                                   headers=_h(admin["token"]))).json()
    assert all(i["role"] == "admin" for i in only_admin["items"])
    assert victim["id"] not in {i["id"] for i in only_admin["items"]}


async def test_create_user_can_log_in(client, admin):
    username = f"t_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/admin/users",
                          json={"username": username, "password": PASSWORD,
                                "role": "staff"},
                          headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["username"] == username and created["role"] == "staff"

    try:
        assert (await _login(client, username)).status_code == 200, "新建的号必须能登录"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", created["id"])


async def test_user_crud_emits_audit_events(client, admin, caplog):
    username = f"t_{uuid.uuid4().hex[:8]}"
    created_id = None
    try:
        with caplog.at_level(logging.INFO, logger="app.audit"):
            r = await client.post(
                "/api/admin/users",
                json={"username": username, "password": PASSWORD, "role": "student"},
                headers=_h(admin["token"]))
            assert r.status_code == 200, r.text
            created_id = r.json()["id"]

            r = await client.patch(
                f"/api/admin/users/{created_id}", json={"role": "staff"},
                headers=_h(admin["token"]))
            assert r.status_code == 200, r.text

            r = await client.post(
                f"/api/admin/users/{created_id}/reset-password",
                json={"password": PASSWORD}, headers=_h(admin["token"]))
            assert r.status_code == 200, r.text

            r = await client.delete(
                f"/api/admin/users/{created_id}", headers=_h(admin["token"]))
            assert r.status_code == 200, r.text

        events = {r.event: r for r in caplog.records
                  if getattr(r, "event", "").startswith("audit.user.")}
        assert events["audit.user.create"].target_id == created_id
        assert events["audit.user.create"].actor_id == admin["id"]
        assert events["audit.user.update"].changed_fields == ["role"]
        assert events["audit.user.reset_password"].target_id == created_id
        assert events["audit.user.delete"].target_id == created_id
    finally:
        if created_id:
            async with db.tx() as conn:
                await conn.execute("DELETE FROM users WHERE id = $1", created_id)


async def test_create_duplicate_username_is_conflict(client, admin, victim):
    r = await client.post("/api/admin/users",
                          json={"username": victim["username"], "password": PASSWORD,
                                "role": "student"},
                          headers=_h(admin["token"]))
    assert r.status_code == 409, r.text


async def test_create_with_invalid_role_is_rejected(client, admin):
    r = await client.post("/api/admin/users",
                          json={"username": f"t_{uuid.uuid4().hex[:8]}",
                                "password": PASSWORD, "role": "teacher"},
                          headers=_h(admin["token"]))
    assert r.status_code == 422, r.text


async def test_create_never_returns_password_hash(client, admin):
    username = f"t_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/admin/users",
                          json={"username": username, "password": PASSWORD,
                                "role": "student"},
                          headers=_h(admin["token"]))
    try:
        assert "password" not in r.text and "hash" not in r.text
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", r.json()["id"])


# ============================================================
# 改角色 / 停用 / 重置口令 —— 三处写操作都要撤销令牌
# ============================================================

async def test_patch_role_revokes_old_token(client, admin, victim):
    """★ 改了角色，**旧令牌必须立刻失效** —— 否则降级成 student 的人
    还能拿着旧 admin 令牌继续用。"""
    assert (await client.get("/api/auth/me", headers=_h(victim["token"]))).status_code == 200

    r = await client.patch(f"/api/admin/users/{victim['id']}", json={"role": "staff"},
                           headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "staff"

    after = await client.get("/api/auth/me", headers=_h(victim["token"]))
    assert after.status_code == 401, "改角色后旧令牌仍然可用 —— token_version 没自增"


async def test_patch_role_new_login_has_new_role(client, admin, victim):
    await client.patch(f"/api/admin/users/{victim['id']}", json={"role": "staff"},
                       headers=_h(admin["token"]))

    fresh = await _login(client, victim["username"])
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["user"]["role"] == "staff"


async def test_disable_user_cannot_log_in_until_enabled(client, admin, victim):
    """★ 停用（§4.3 用户管理）。库里原本没有这个字段 —— 见文件头说明。"""
    r = await client.patch(f"/api/admin/users/{victim['id']}", json={"is_active": False},
                           headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False

    assert (await _login(client, victim["username"])).status_code == 401, "停用后还能登录"
    assert (await client.get("/api/auth/me",
                             headers=_h(victim["token"]))).status_code == 401, \
        "停用后旧令牌仍然可用"

    r = await client.patch(f"/api/admin/users/{victim['id']}", json={"is_active": True},
                           headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    assert (await _login(client, victim["username"])).status_code == 200, "重新启用后应能登录"


async def test_wrong_password_on_disabled_account_does_not_leak(client, admin, victim):
    """口令错的人拿到的仍是「用户名或口令不正确」——
    停用与否只在**口令正确之后**才判，不泄露账号是否存在。"""
    r = await client.patch(f"/api/admin/users/{victim['id']}", json={"is_active": False},
                           headers=_h(admin["token"]))
    assert r.status_code == 200, r.text
    # 先证明停用真的生效（口令正确也进不来）—— 否则下面那条断言今天是白过的
    assert (await _login(client, victim["username"])).status_code == 401

    r = await _login(client, victim["username"], password="WrongPass@1")
    assert r.status_code == 401
    assert "停用" not in r.text and "disabled" not in r.text.lower()


async def test_reset_password_revokes_old_token_and_sets_new(client, admin, victim):
    """★ 重置口令同样要撤销 —— 否则「改了密码但旧令牌还能用」。"""
    new_password = "NewPass@98765"

    r = await client.post(f"/api/admin/users/{victim['id']}/reset-password",
                          json={"password": new_password}, headers=_h(admin["token"]))
    assert r.status_code == 200, r.text

    assert (await client.get("/api/auth/me",
                             headers=_h(victim["token"]))).status_code == 401
    assert (await _login(client, victim["username"],
                         password=new_password)).status_code == 200
    assert (await _login(client, victim["username"])).status_code == 401, "旧口令必须失效"


async def test_patch_unknown_user_is_404(client, admin):
    r = await client.patch(f"/api/admin/users/{uuid.uuid4().hex}", json={"role": "staff"},
                           headers=_h(admin["token"]))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


# ============================================================
# 评审 M4-K4：refresh 也必须看 is_active
# ============================================================

async def test_refresh_rejected_for_disabled_account(client, victim):
    """★ 停用后，**即使令牌版本没变**，`/refresh` 也必须拒绝。

    经接口停用会自增 `token_version`（那条路本来就被 tv 比对堵住了），
    所以这里**直接改库**模拟「版本没变、但账号被停用」—— 那正是剩下的口子：
    `/refresh` 只比 tv，完全不看 `is_active`，被停用的账号能一直换出新的
    7 天 access。日后任何一处新增的「写 users」路径忘了自增 tv，同一个口子就开了。

    注：这条与「停用后旧 access 立刻 401」不重复 —— 那条走的是 tv，
    这条走的是 is_active，两条路各堵各的。
    """
    fresh = await _login(client, victim["username"])
    refresh_token = fresh.json()["refresh_token"]

    # 直接改库：只置停用，**不动 token_version**
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET is_active = FALSE WHERE id = $1",
                           victim["id"])
        tv = await conn.fetchval("SELECT token_version FROM users WHERE id = $1",
                                 victim["id"])
    assert tv == int(pyjwt_decode_tv(refresh_token)), "前置：令牌版本没变（正是要测的场景）"

    r = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})

    assert r.status_code == 401, f"被停用的账号还能刷新出新的 access：{r.text}"


def pyjwt_decode_tv(token: str) -> str:
    """只取 payload 里的 tv，不验签（测试里自己造的令牌，不需要验）。"""
    import base64
    import json as _json
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return str(_json.loads(base64.urlsafe_b64decode(payload))["tv"])


# ============================================================
# 删除用户（§4.3 用户管理）
# ============================================================

async def test_delete_user_is_admin_only(client, admin, victim):
    """普通用户不能删号 —— 尤其是**不能删管理员**。"""
    r = await client.delete(f"/api/admin/users/{admin['id']}",
                            headers=_h(victim["token"]))
    assert r.status_code == 403, r.text
    async with db.tx() as conn:
        alive = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", admin["id"])
    assert alive, "返回 403 了，管理员却还是被删了"


async def test_delete_unknown_user_is_404(client, admin):
    r = await client.delete(f"/api/admin/users/{uuid.uuid4().hex}",
                            headers=_h(admin["token"]))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


async def test_delete_self_is_rejected(client, admin):
    """★ 不能删自己。

    不只是手滑保护：拦掉这条之后，**管理员永远不可能被删光**。
    能发出这个请求的人，一定是当下活着、且持有有效令牌的管理员
    （令牌有效 ⇒ 登录时账号是启用的）；他删得掉别人，删不掉自己，
    所以他必然还在。因此**不需要**再写一条「不许删最后一个管理员」的特判。
    """
    r = await client.delete(f"/api/admin/users/{admin['id']}",
                            headers=_h(admin["token"]))
    assert r.status_code == 400, r.text
    async with db.tx() as conn:
        alive = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", admin["id"])
    assert alive, "拦是拦了，人还是没了"


async def test_delete_user_removes_account_and_history(client, admin, target):
    """★ 删号连带清掉他的会话与消息。

    `conversations.user_id` 是 NOT NULL 外键 —— 不清就删不动，
    这正是「有历史就不许删」那个备选方案的由来（本方案选了连清）。
    """
    conv_id = target["conversation_id"]

    r = await client.delete(f"/api/admin/users/{target['id']}",
                            headers=_h(admin["token"]))
    assert r.status_code == 200, r.text

    async with db.tx() as conn:
        user = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", target["id"])
        conv = await conn.fetchval("SELECT 1 FROM conversations WHERE id = $1", conv_id)
        left = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE conversation_id = $1", conv_id)

    assert user is None, "用户行还在"
    assert conv is None, "会话没删掉"
    assert left == 0, "会话删了，消息还留着（成了孤儿行）"
    assert (await _login(client, target["username"])).status_code == 401, "删了还能登录"


async def test_delete_user_keeps_documents_eval_sets_and_logs(client, admin, target):
    """★ 删的是人，不是他的产出：文档、评测集、qa_logs 都留下。

    一个学生毕业，全校语料不能跟着少几份 —— 共享知识必须留下，
    归属列置空即可（文档管理页的「上传人」会显示为空）。
    这里**必须同时断言「行还在」**：只断言 `uploader_id is None` 的话，
    「整个文档被连带删了」也会让那条断言通过。
    """
    r = await client.delete(f"/api/admin/users/{target['id']}",
                            headers=_h(admin["token"]))
    assert r.status_code == 200, r.text

    async with db.tx() as conn:
        doc = await conn.fetchrow("SELECT uploader_id FROM documents WHERE id = $1",
                                  target["document_id"])
        ev = await conn.fetchrow("SELECT created_by FROM eval_sets WHERE id = $1",
                                 target["eval_set_id"])
        log_user = await conn.fetchval("SELECT user_id FROM qa_logs WHERE id = $1",
                                       target["qa_log_id"])

    assert doc is not None, "文档被连带删了 —— 全校语料不能随人走"
    assert doc["uploader_id"] is None, "文档还在，上传人归属没置空"
    assert ev is not None, "评测集被连带删了"
    assert ev["created_by"] is None, "评测集还在，创建人归属没置空"
    assert log_user == target["id"], "qa_logs 是审计日志，不该被删也不该被改"


async def test_delete_user_invalidates_their_token(client, admin, target):
    """删完人，他手上的令牌立刻作废。

    ⚠️ 这条路走的是「查库查不到用户」兜底（`deps.current_user`），
       **不是** token_version 比对 —— 删除是本文件里唯一**不**自增 tv
       的写操作：行都要没了，自增无处可增。两条路各堵各的。
    """
    assert (await client.get("/api/auth/me",
                             headers=_h(target["token"]))).status_code == 200

    r = await client.delete(f"/api/admin/users/{target['id']}",
                            headers=_h(admin["token"]))
    assert r.status_code == 200, r.text

    after = await client.get("/api/auth/me", headers=_h(target["token"]))
    assert after.status_code == 401, "人删了，旧令牌还能用"


async def test_deleted_username_can_be_reused(client, admin, target):
    """真删与软删的分水岭：用户名立刻能被重新注册。

    软删（行留着、只加 `deleted_at`）会被 `users.username` 的 UNIQUE
    一直挡着，想复用还得再去动约束 —— 那是另一个方案，本方案选择了真删。
    """
    r = await client.delete(f"/api/admin/users/{target['id']}",
                            headers=_h(admin["token"]))
    assert r.status_code == 200, r.text

    again = await client.post("/api/admin/users",
                              json={"username": target["username"],
                                    "password": PASSWORD, "role": "student"},
                              headers=_h(admin["token"]))
    assert again.status_code == 200, f"用户名没被释放：{again.text}"
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", again.json()["id"])
