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
