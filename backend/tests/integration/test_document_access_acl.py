"""User 端原文访问的 ACL（§8.2 M3-5 的测试点）。

每条用例都在防同一件事：**`/api/documents/{id}/file` 成为绕过检索期 ACL 的捷径**。
「搜不到」必须等于「下不到」，而且——

⚠️ **不可见与不存在必须不可区分（都是 404）**：403 等于告诉对方
   「这份文档存在，只是你没权限」，可以被拿来探测文档存在性。

⚠️ 三个接口都要测：只测 `/file` 的话，`/text` 与 `/images` 各写一套判定的
   那天不会有人发现 —— 而它们读的是同一份原文。

配一条**正向对照**（有权用户确实下得到）证明本文件不是空转：
全是 404 的测试，分不清「ACL 生效」和「路由压根没挂上」。
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid

import httpx
import pytest_asyncio

from app import db
from app.api import document_access as DA
from app.core.config import data_dir
from app.core.security import hash_password
from app.main import app
from tests.support import ingest_text_doc, purge_documents

PASSWORD = "Test@12345"
IMAGE_NAME = "p1_0.jpeg"


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def users():
    """一个 student、一个 admin —— 都要能真登录（JWT 是唯一身份来源）。"""
    created: dict[str, dict] = {}
    for role in ("student", "admin"):
        uid = uuid.uuid4().hex
        username = f"t_{role}_{uid[:6]}"
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO users (id, username, password_hash, role, token_version) "
                "VALUES ($1, $2, $3, $4, 0)",
                uid, username, hash_password(PASSWORD), role,
            )
        created[role] = {"id": uid, "username": username}
    yield created
    for user in created.values():
        async with db.tx() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest_asyncio.fixture
async def docs(users):
    """一份 admin 专属文档 + 一份公开文档，外加一张图。"""
    admin_id = users["admin"]["id"]
    suffix = uuid.uuid4().hex[:8]
    t_restricted = f"原文访问_{suffix}_restricted"
    t_staff = f"原文访问_{suffix}_staff"
    t_public = f"原文访问_{suffix}_public"

    restricted = await ingest_text_doc(
        admin_id=admin_id, title=t_restricted, text="受限文档正文。" * 40,
        visibility="restricted", visible_roles=["admin"],
    )
    # ⚠️ 提权只在「本角色看不见」时才触发：admin 对自己可见的文档走正常公式，
    #    所以必须另有一份 **admin 看不见** 的文档（只勾了 staff）来测提权。
    staff_only = await ingest_text_doc(
        admin_id=admin_id, title=t_staff, text="仅教职工可见的正文。" * 40,
        visibility="restricted", visible_roles=["staff"],
    )
    public = await ingest_text_doc(
        admin_id=admin_id, title=t_public, text="公开文档正文。" * 40,
    )

    image_dirs = []
    for doc_id in (staff_only, public):
        image_dir = data_dir() / "extracted_images" / doc_id
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / IMAGE_NAME).write_bytes(b"\xff\xd8\xff\xe0spike-jpeg\xff\xd9")
        image_dirs.append(image_dir)

    try:
        yield {"restricted": restricted, "staff_only": staff_only, "public": public,
               "titles": [t_restricted, t_staff, t_public]}
    finally:
        # 先清文档再删用户 —— documents.uploader_id 有外键
        await purge_documents([t_restricted, t_staff, t_public])
        for image_dir in image_dirs:
            shutil.rmtree(image_dir, ignore_errors=True)


async def _auth(client: httpx.AsyncClient, username: str) -> dict:
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _three_paths(document_id: str) -> list[str]:
    return [
        f"/api/documents/{document_id}/file",
        f"/api/documents/{document_id}/text",
        f"/api/documents/{document_id}/images/{IMAGE_NAME}",
    ]


# ============================================================
# 不可见 = 不存在 = 404（且不可区分）
# ============================================================

async def test_student_gets_404_on_every_endpoint(client, users, docs):
    """★ student 拿 admin 专属文档的 id → 三个接口**全是 404**。"""
    headers = await _auth(client, users["student"]["username"])

    for path in _three_paths(docs["restricted"]):
        r = await client.get(path, headers=headers)
        assert r.status_code == 404, f"{path} 泄露了受限文档的存在性（{r.status_code}）"


async def test_missing_document_is_indistinguishable(client, users, docs):
    """不存在的 id 与不可见的 id，响应**逐字相同** —— 否则可以用来探测。"""
    headers = await _auth(client, users["student"]["username"])
    ghost = uuid.uuid4().hex

    for missing, hidden in zip(_three_paths(ghost), _three_paths(docs["restricted"])):
        r_missing = await client.get(missing, headers=headers)
        r_hidden = await client.get(hidden, headers=headers)
        assert r_missing.status_code == r_hidden.status_code == 404
        assert r_missing.json() == r_hidden.json(), "两种 404 的响应体不一样，能探测出文档存在"


async def test_non_admin_escalation_flag_is_ignored(client, users, docs):
    """非 admin 传 `include_restricted=true`：按未传处理，**不报错、不记提权**。"""
    headers = await _auth(client, users["student"]["username"])

    for path in _three_paths(docs["restricted"]):
        r = await client.get(f"{path}?include_restricted=true", headers=headers)
        assert r.status_code == 404, f"{path} 让非 admin 提权成功了"


# ============================================================
# 正向对照：有权就下得到（反空转）
# ============================================================

async def test_visible_document_downloads_fine(client, users, docs):
    """★ 反空转：公开文档 200、内容非空、且**不标** escalated。"""
    headers = await _auth(client, users["student"]["username"])

    r = await client.get(f"/api/documents/{docs['public']}/file", headers=headers)

    assert r.status_code == 200
    assert r.content
    assert "x-escalated" not in {k.lower() for k in r.headers}


async def test_chinese_filename_does_not_break_download(client, users, docs):
    """文件名是中文（语料全是中文）—— Content-Disposition 必须编得住。"""
    headers = await _auth(client, users["student"]["username"])

    r = await client.get(f"/api/documents/{docs['public']}/file", headers=headers)

    disposition = r.headers.get("content-disposition", "")
    assert "filename" in disposition.lower()
    assert "utf-8''" in disposition.lower() or "%" in disposition, (
        "中文文件名没有按 RFC 5987 编码，下载下来会变成乱码或直接报错"
    )


# ============================================================
# 提权：拿得到 + 标出来 + 留痕
# ============================================================

async def test_admin_escalation_is_marked_and_audited(client, users, docs, caplog):
    """★ admin 提权 → 200 + `X-Escalated` + **审计日志**（谁、看了哪份）。"""
    headers = await _auth(client, users["admin"]["username"])

    with caplog.at_level(logging.WARNING, logger="app.api.document_access"):
        r = await client.get(f"/api/documents/{docs['staff_only']}/file"
                             f"?include_restricted=true", headers=headers)

    assert r.status_code == 200
    assert r.content
    assert r.headers.get("x-escalated") == "true", "越权取得必须能一眼看出来"

    audits = [rec for rec in caplog.records
              if getattr(rec, "event", "") == "document_access.escalated"]
    assert len(audits) == 1, "提权必须留痕"
    assert audits[0].document_id == docs["staff_only"]
    assert audits[0].user_role == "admin"
    assert audits[0].action == "file"


async def test_admin_without_escalation_still_gets_404(client, users, docs):
    """★ admin **不提权**时按普通公式判 —— 不能变成旁路。"""
    headers = await _auth(client, users["admin"]["username"])

    r = await client.get(f"/api/documents/{docs['staff_only']}/file", headers=headers)

    assert r.status_code == 404, "admin 不提权就拿到了 staff 专属文档 = 角色变成旁路"


async def test_escalated_text_access_is_audited_too(client, users, docs, caplog):
    """`/text` 提权同样要留痕 —— 三个接口共用一个判定，别漏掉留痕。"""
    headers = await _auth(client, users["admin"]["username"])

    with caplog.at_level(logging.WARNING, logger="app.api.document_access"):
        r = await client.get(f"/api/documents/{docs['staff_only']}/text"
                             f"?include_restricted=true", headers=headers)

    assert r.status_code == 200
    assert any(getattr(rec, "event", "") == "document_access.escalated"
               and rec.action == "text" for rec in caplog.records)


# ============================================================
# /text 的载荷
# ============================================================

async def test_text_payload_has_offset_index(client, users, docs):
    """`{document_id, text, char_offset_index}`；索引按 chunk_index 升序。"""
    headers = await _auth(client, users["student"]["username"])

    r = await client.get(f"/api/documents/{docs['public']}/text", headers=headers)
    data = r.json()

    assert data["document_id"] == docs["public"]
    assert "公开文档正文" in data["text"]
    index = data["char_offset_index"]
    assert index, "偏移索引是 /text 的存在意义 —— 空的就是没实现"
    assert [row["chunk_index"] for row in index] == sorted(row["chunk_index"] for row in index)
    assert all({"chunk_id", "char_start", "char_end", "page"} <= set(row) for row in index)


# ============================================================
# 图片：签名 URL
# ============================================================

async def test_signed_image_url_works_without_jwt(client, users, docs):
    """★ `<img src>` 带不上 JWT：签名 URL 必须**自证身份**。"""
    headers = await _auth(client, users["student"]["username"])

    r = await client.get(f"/api/documents/{docs['public']}/images/{IMAGE_NAME}",
                         headers=headers)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert r.json()["expires_at"] > time.time()

    # 关键一步：不带任何请求头去取图
    img = await client.get(url)
    assert img.status_code == 200
    assert img.content.startswith(b"\xff\xd8\xff")


async def test_expired_signature_is_rejected(client, docs):
    """★ 过期 → 403（§8.2 M3-5 测试点⑤）。"""
    expired = int(time.time()) - 10
    sig = DA._sign(docs["restricted"], IMAGE_NAME, expired)

    r = await client.get(f"/api/documents/{docs['restricted']}/images/{IMAGE_NAME}"
                         f"?exp={expired}&sig={sig}")

    assert r.status_code == 403


async def test_signature_is_bound_to_document_and_name(client, docs):
    """签名绑死「文档 + 文件名」：换个文档 / 换个文件名都过不了。"""
    future = int(time.time()) + 300
    sig = DA._sign(docs["public"], IMAGE_NAME, future)

    swapped_doc = await client.get(
        f"/api/documents/{docs['restricted']}/images/{IMAGE_NAME}?exp={future}&sig={sig}")
    swapped_name = await client.get(
        f"/api/documents/{docs['public']}/images/other.jpeg?exp={future}&sig={sig}")

    assert swapped_doc.status_code == 403
    assert swapped_name.status_code == 403


async def test_image_name_cannot_escape_its_directory(client, users, docs):
    """路径穿越挡在 `_SAFE_NAME` 上 —— 别让 `../` 读到别人文档的图。"""
    headers = await _auth(client, users["admin"]["username"])

    for evil in ("..%2F..%2Fsecret.jpeg", "..%5Csecret.jpeg", "../secret.jpeg"):
        r = await client.get(f"/api/documents/{docs['restricted']}/images/{evil}",
                             headers=headers)
        assert r.status_code in (403, 404), f"{evil} 没被挡住"


def test_image_name_guard_rejects_traversal_directly():
    assert DA._SAFE_NAME.match(IMAGE_NAME)
    for evil in ("../x.jpeg", "..\\x.jpeg", "a/b.jpeg", "", None):
        assert not DA._SAFE_NAME.match(evil or "")


# ============================================================
# 静态挂载必须不存在
# ============================================================

def test_no_static_images_mount():
    """★ `main.py` 里**不能**有 `/images` 静态挂载 —— 那是把受限文档的图直接裸露。"""
    mounted = [getattr(route, "path", "") for route in app.routes]
    assert "/images" not in mounted
