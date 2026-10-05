"""认证与令牌撤销（§3.7.1）。

对应施工计划 M0-4 的测试点：
  ① 登录拿到两个 token  ② access 访问 /me 200  ③ logout 后同一 access 立刻 401
  ④ refresh 换新 access 200  ⑤ 过期的 access 401
外加：refresh 也必须被撤销（tv 匹配）、错误口令不泄露用户名是否存在。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio

from app import db
from app.core.config import cfg, secret
from app.core.security import hash_password
from app.main import app

PASSWORD = "Test@12345"


@pytest_asyncio.fixture
async def user():
    """建一个可登录的测试用户，用完删掉（连带其令牌版本）。"""
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, 'student', 0)",
            user_id, username, hash_password(PASSWORD),
        )
    yield {"id": user_id, "username": username}
    async with db.tx() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client: httpx.AsyncClient, username: str) -> dict:
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================

async def test_login_returns_token_pair_and_user(client, user):
    data = await _login(client, user["username"])
    assert data["access_token"] and data["refresh_token"]
    assert data["expires_in"] == int(cfg("auth.access_token_minutes", 15)) * 60
    assert data["user"]["username"] == user["username"]
    assert data["user"]["role"] == "student"


async def test_wrong_password_rejected(client, user):
    r = await client.post("/api/auth/login",
                          json={"username": user["username"], "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_unknown_user_gives_same_error_as_wrong_password(client):
    """不用错误响应泄露用户名是否存在。"""
    r = await client.post("/api/auth/login",
                          json={"username": "no_such_user_xyz", "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_me_with_access_token(client, user):
    data = await _login(client, user["username"])
    r = await client.get("/api/auth/me",
                         headers={"Authorization": f"Bearer {data['access_token']}"})
    assert r.status_code == 200
    assert r.json()["id"] == user["id"]


async def test_me_without_token_is_401(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


async def test_refresh_issues_new_access_and_does_not_rotate(client, user):
    data = await _login(client, user["username"])
    r = await client.post("/api/auth/refresh",
                          json={"refresh_token": data["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    # refresh_token 原样返回，不轮换（§3.7.1）
    assert new["refresh_token"] == data["refresh_token"]

    r2 = await client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {new['access_token']}"})
    assert r2.status_code == 200


async def test_access_token_cannot_be_used_as_refresh(client, user):
    data = await _login(client, user["username"])
    r = await client.post("/api/auth/refresh",
                          json={"refresh_token": data["access_token"]})
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized" or "类型" in r.json()["message"]


async def test_logout_revokes_access_token_immediately(client, user):
    """★ 撤销的核心断言：登出后，**同一个** access token 立刻失效。"""
    data = await _login(client, user["username"])
    headers = {"Authorization": f"Bearer {data['access_token']}"}

    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    r = await client.post("/api/auth/logout", headers=headers)
    assert r.status_code == 200

    r2 = await client.get("/api/auth/me", headers=headers)
    assert r2.status_code == 401, "登出后令牌仍然可用 —— 撤销没生效"
    assert r2.json()["code"] == "token_revoked"


async def test_logout_revokes_refresh_token_too(client, user):
    """登出是用户级撤销：access 与 refresh 都失效。"""
    data = await _login(client, user["username"])
    await client.post("/api/auth/logout",
                      headers={"Authorization": f"Bearer {data['access_token']}"})

    r = await client.post("/api/auth/refresh",
                          json={"refresh_token": data["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["code"] == "token_revoked"


async def test_relogin_after_logout_works(client, user):
    """撤销后重新登录应拿到可用令牌（token_version 已自增，新令牌带新 tv）。"""
    data = await _login(client, user["username"])
    await client.post("/api/auth/logout",
                      headers={"Authorization": f"Bearer {data['access_token']}"})

    fresh = await _login(client, user["username"])
    r = await client.get("/api/auth/me",
                         headers={"Authorization": f"Bearer {fresh['access_token']}"})
    assert r.status_code == 200


async def test_expired_access_token_is_401(client, user):
    """手工造一个已过期的令牌。"""
    expired = pyjwt.encode(
        {
            "sub": user["id"], "role": "student", "tv": 0, "type": "access",
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        secret("auth.jwt_secret"),
        algorithm=cfg("auth.jwt_algorithm", "HS256"),
    )
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["code"] == "token_expired"


async def test_token_with_stale_tv_is_rejected(client, user):
    """令牌的 tv 与实际不符（模拟他处已撤销）→ 401。"""
    data = await _login(client, user["username"])
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE id = $1", user["id"])
    r = await client.get("/api/auth/me",
                         headers={"Authorization": f"Bearer {data['access_token']}"})
    assert r.status_code == 401
    assert r.json()["code"] == "token_revoked"
