"""管理端文档接口（§3.7.3 管理端 / §4.3.1 / §4.3.2）。

⚠️ **本文件最要紧的一条**：改 `visibility` / `effective_date` / `status`
   必须**真的把 Chroma 的 chunk metadata 重写掉**（§4.3.1.2）。
   这三个字段冗余在向量库里，改 PostgreSQL 不会自动同步，而检索期的
   过滤读的正是 Chroma。所以「PG 里那一行改了」是个**测不出缺陷的断言** ——
   必须验到**检索行为变了**：改前学生搜得到、改完立刻搜不到。

⚠️ `title` 是唯一**不触发**重索引的字段（§3.7.2 侧的管理端写接口规格）。

⚠️ 不可改的字段（`file_type` / `md5` / `version` / `doc_group_id`）**必须报错**，
   不能静默忽略 —— 静默忽略会让管理员以为改成功了。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest_asyncio

from app import db
from app.core.deps import UserContext
from app.core.security import hash_password
from app.main import app
from app.retrieval import bm25, vector
from app.retrieval.search import vector_retrieve
from tests.support import (
    delete_admin,
    ingest_text_doc,
    make_user,
    purge_documents,
    purge_documents_by_id,
)

PASSWORD = "Test@12345"
MARKER = "钴蓝色飞艇"


def _marker() -> str:
    """**每份测试文档一个唯一关键词**。

    ⚠️ 不要所有用例共用同一个 MARKER：正文里的关键词必须唯一，检索才**必然**
       把这份文档排在最前 —— 否则一旦索引里残留了别的同样含该短语的 chunk，
       Top-K 被占满，`_student_retrieves` 的正控就会红
       （实测：三个 `test_patch_*_removes_from_retrieval` 同时红在正控上）。
       这是 M1 的 ACL 用例早就立下的做法（「用例的正文是合成的唯一短语」）。
    """
    return f"{MARKER}{uuid.uuid4().hex[:6]}"


def _body(tag: str, marker: str) -> str:
    return (
        f"【管理端接口测试专用文档 {tag}】\n\n"
        f"本文含唯一关键词「{marker}」，用于验证可见范围变更是否真的写进了向量库。\n"
        f"再次出现「{marker}」以便分块后仍然集中。\n"
        + "本段是为了凑足分块长度而重复的正文，不含其他关键词。\n" * 12
    )


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def raw_client():
    """**注入故障的用例专用**。

    ⚠️ httpx 的 `ASGITransport` 默认 `raise_app_exceptions=True`：
        Starlette 处理完异常、发出 500 之后会把它**再抛一次**给调用方 ——
        于是用例拿到的是 RuntimeError 而不是响应，状态码根本测不到。
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_user(role: str) -> dict:
    user_id = uuid.uuid4().hex
    username = f"t_{user_id[:8]}"
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, $3, $4, 0)",
            user_id, username, hash_password(PASSWORD), role,
        )
    return {"id": user_id, "username": username}


async def _login(client: httpx.AsyncClient, username: str) -> str:
    r = await client.post("/api/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest_asyncio.fixture
async def admin(client):
    u = await _create_user("admin")
    u["token"] = await _login(client, u["username"])
    yield u
    await delete_admin(u["id"])


@pytest_asyncio.fixture
async def student(client):
    u = await _create_user("student")
    u["token"] = await _login(client, u["username"])
    yield u
    await delete_admin(u["id"])


def _h(u: dict) -> dict:
    return {"Authorization": f"Bearer {u['token']}"}


@pytest_asyncio.fixture
async def doc(admin):
    """一份**公开**的合成文档（默认可被 student 检索到，用于做正向对照）。"""
    title = f"管理端测试_{uuid.uuid4().hex[:8]}"
    marker = _marker()
    doc_id = await ingest_text_doc(admin_id=admin["id"], title=title,
                                   text=_body(title, marker), visibility="public")
    try:
        yield {"id": doc_id, "title": title, "marker": marker}
    finally:
        # ⚠️ 按 **id** 清：这个夹具服务的用例里有会改标题的
        #    （`test_patch_title_does_not_reindex`），按 title 清会静默漏孤儿
        await purge_documents_by_id([doc_id])


async def _student_retrieves(doc_id: str, marker: str) -> bool:
    """student 走**向量路**能不能拿到这份文档 —— 真实的 `build_where` + Chroma。

    ⚠️ query 用**这份文档自己的**唯一关键词：共用短语的话，
       索引里任何残留的同短语 chunk 都能把 Top-K 占满，
       正控会红在「本该第一的没出现」上。
    """
    async with db.tx() as conn:
        chunks = await vector_retrieve(conn, marker, make_user("student"))
    return doc_id in {c.document_id for c in chunks}


def _meta(doc_id: str) -> dict:
    rows = vector.get_chunks(doc_id, limit=100000)
    assert rows, f"向量库里没有 {doc_id} 的 chunk —— 索引没建起来"
    return rows[0]["metadata"]


def _bm25_hits(chunk_ids: list[str], marker: str) -> set[str]:
    """BM25 索引里**还活着**的那部分 chunk_id。

    直接问索引（而不是读映射表文件）：孤儿条目的危害就是「还能被搜出来」，
    所以判据也应该是「还搜不搜得到」。
    """
    hits = bm25.search(marker, k=50)
    return {h.chunk_id for h in hits} & set(chunk_ids)


# ============================================================
# 列表与筛选（§4.3.1.1）
# ============================================================

async def test_list_returns_pagination_envelope(client, admin, doc):
    r = await client.get("/api/admin/documents", headers=_h(admin))
    assert r.status_code == 200, r.text
    body = r.json()

    for key in ("items", "total", "page", "page_size", "has_more"):
        assert key in body, f"列表响应缺少 {key}"
    mine = [i for i in body["items"] if i["id"] == doc["id"]]
    assert mine, "刚入库的文档必须出现在列表里"
    item = mine[0]
    for key in ("id", "doc_group_id", "title", "filename", "version", "status",
                "visibility", "visible_roles", "effective_date", "chunk_count",
                "is_current"):
        assert key in item, f"列表项缺少 {key}（管理端表格要用）"


async def test_list_filters_by_status(client, admin, doc):
    """`status` 取 active / disabled / all，默认 all（§4.3.1.1）。"""
    assert (await client.patch(f"/api/admin/documents/{doc['id']}",
                               json={"status": "disabled"}, headers=_h(admin))
            ).status_code == 200

    active = (await client.get("/api/admin/documents?status=active",
                               headers=_h(admin))).json()
    disabled = (await client.get("/api/admin/documents?status=disabled",
                                 headers=_h(admin))).json()
    every = (await client.get("/api/admin/documents?status=all",
                              headers=_h(admin))).json()

    assert doc["id"] not in {i["id"] for i in active["items"]}
    assert doc["id"] in {i["id"] for i in disabled["items"]}
    assert doc["id"] in {i["id"] for i in every["items"]}


async def test_list_filters_by_visibility(client, admin, doc):
    public = (await client.get("/api/admin/documents?visibility=public",
                               headers=_h(admin))).json()
    restricted = (await client.get("/api/admin/documents?visibility=restricted",
                                   headers=_h(admin))).json()

    assert doc["id"] in {i["id"] for i in public["items"]}
    assert doc["id"] not in {i["id"] for i in restricted["items"]}


async def test_list_q_matches_title_and_filename(client, admin, doc):
    """`q` 模糊匹配**标题与原始文件名**（§4.3.1.1）。"""
    by_title = (await client.get(f"/api/admin/documents?q={doc['title']}",
                                 headers=_h(admin))).json()
    assert doc["id"] in {i["id"] for i in by_title["items"]}

    miss = (await client.get("/api/admin/documents?q=绝对不存在的标题xyz",
                             headers=_h(admin))).json()
    assert doc["id"] not in {i["id"] for i in miss["items"]}


async def test_list_page_size_upper_bound_enforced(client, admin):
    """全站分页上限 100（§4.3.1.1）—— 超了要拒，不能悄悄按 100 返回。"""
    r = await client.get("/api/admin/documents?page_size=101", headers=_h(admin))
    assert r.status_code == 422, r.text


async def test_student_cannot_use_admin_api(client, student):
    r = await client.get("/api/admin/documents", headers=_h(student))
    assert r.status_code == 403, r.text


# ============================================================
# 版本（§4.3.1 版本管理）
# ============================================================

async def test_versions_lists_all_and_flags_current(client, admin):
    """同 title 传两次 = 同组两个版本；当前生效版本要标出来（§3.3.1 的折叠规则）。"""
    title = f"版本测试_{uuid.uuid4().hex[:8]}"
    v1 = await ingest_text_doc(admin_id=admin["id"], title=title,
                               text=_body(title + "v1", _marker()))
    v2 = await ingest_text_doc(admin_id=admin["id"], title=title,
                               text=_body(title + "v2", _marker()))
    try:
        group = (await client.get("/api/admin/documents", headers=_h(admin))).json()
        gid = next(i["doc_group_id"] for i in group["items"] if i["id"] == v1)

        r = await client.get(f"/api/admin/documents/{gid}/versions", headers=_h(admin))
        assert r.status_code == 200, r.text
        versions = r.json()["versions"]

        assert [v["version"] for v in versions] == [2, 1], "版本按倒序，当前生效在最上面"
        current = [v for v in versions if v["is_current"]]
        assert len(current) == 1 and current[0]["id"] == v2, \
            "只有组内最大 version 且 active 的那一版才算当前生效"
    finally:
        await purge_documents([title])


async def test_versions_of_unknown_group_is_404(client, admin):
    r = await client.get(f"/api/admin/documents/{uuid.uuid4().hex}/versions",
                         headers=_h(admin))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found", "必须是业务 404，不是「路由不存在」的 404"


# ============================================================
# 分块预览（§4.3.2）
# ============================================================

async def test_chunks_preview_exposes_metadata(client, admin, doc):
    """§4.3.2 列了 8 个字段 —— 这是**唯一**能看见 `char_start` / `vis_*` 的地方。"""
    r = await client.get(f"/api/admin/documents/{doc['id']}/chunks", headers=_h(admin))
    assert r.status_code == 200, r.text
    chunks = r.json()["chunks"]

    assert len(chunks) > 0
    first = chunks[0]
    for key in ("chunk_index", "page", "current_chapter", "chapter_level",
                "char_start", "char_end", "text", "image_paths"):
        assert key in first, f"分块预览缺少 {key}"
    assert [c["chunk_index"] for c in chunks] == sorted(c["chunk_index"] for c in chunks)
    assert chunks[0]["text"], "分块正文不能是空的"


async def test_chunks_preview_of_unknown_document_is_404(client, admin):
    r = await client.get(f"/api/admin/documents/{uuid.uuid4().hex}/chunks",
                         headers=_h(admin))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


# ============================================================
# 改字段：哪些重索引、哪些不重（§3.7.2 侧的管理端写接口规格）
# ============================================================

async def test_patch_title_does_not_reindex(client, admin, doc, monkeypatch):
    """`title` 不触发重索引 —— 重写全部 chunk 的 metadata 是有代价的，
    改个显示名不该付这个代价。"""
    from app.services import index_service

    calls = []
    monkeypatch.setattr(index_service, "rewrite_metadata",
                        lambda *a, **k: calls.append((a, k)) or 0)

    r = await client.patch(f"/api/admin/documents/{doc['id']}",
                           json={"title": "改过的标题"}, headers=_h(admin))

    assert r.status_code == 200, r.text
    assert r.json()["title"] == "改过的标题"
    assert calls == [], "改 title 不该触发重索引"


async def test_patch_visibility_reindexes_chroma_and_hides_from_student(client, admin, doc):
    """★★ 本文件的核心：改可见范围必须**真的**写进 Chroma，学生立刻搜不到。

    只断言「PG 那一行变了」是测不出这个缺陷的 —— 检索期的 ACL 读的是
    Chroma 的 `visibility` / `vis_*` 字段。
    """
    assert await _student_retrieves(doc["id"], doc["marker"]), "正向对照：改之前学生本来就该搜得到"

    r = await client.patch(f"/api/admin/documents/{doc['id']}",
                           json={"visibility": "restricted", "visible_roles": ["admin"]},
                           headers=_h(admin))
    assert r.status_code == 200, r.text

    meta = _meta(doc["id"])
    assert meta["visibility"] == "restricted", "Chroma 的 visibility 没被重写"
    assert meta["vis_student"] is False, "Chroma 的 vis_student 没被重写"
    assert meta["vis_admin"] is True

    assert not await _student_retrieves(doc["id"], doc["marker"]), \
        "改完受限后学生仍然搜得到 —— 重索引没生效"

    # 改回去，学生必须又能搜到（排除「索引整个坏掉」这种假通过）
    assert (await client.patch(f"/api/admin/documents/{doc['id']}",
                               json={"visibility": "public", "visible_roles": []},
                               headers=_h(admin))).status_code == 200
    assert await _student_retrieves(doc["id"], doc["marker"]), "改回公开后学生又该搜得到"


async def test_patch_status_disable_removes_from_retrieval(client, admin, doc):
    """★ `status` 也冗余在 Chroma —— 不重写的话 `status="active"` 过滤形同虚设。

    这是原方案漏掉的一半（只写了 visibility/effective_date）。
    """
    assert await _student_retrieves(doc["id"], doc["marker"])

    r = await client.patch(f"/api/admin/documents/{doc['id']}",
                           json={"status": "disabled"}, headers=_h(admin))
    assert r.status_code == 200, r.text

    assert _meta(doc["id"])["status"] == "disabled", "Chroma 的 status 没被重写"
    assert not await _student_retrieves(doc["id"], doc["marker"]), "停用后仍能被检索到"


async def test_patch_effective_date_future_removes_from_retrieval(client, admin, doc):
    """生效日在未来 → 暂不参与检索（§15 #8，不能出现政策真空期）。"""
    assert await _student_retrieves(doc["id"], doc["marker"])

    future = (date.today() + timedelta(days=30)).isoformat()
    r = await client.patch(f"/api/admin/documents/{doc['id']}",
                           json={"effective_date": future}, headers=_h(admin))
    assert r.status_code == 200, r.text

    assert not await _student_retrieves(doc["id"], doc["marker"]), "未来生效日的文档不该参与检索"


async def test_patch_rejects_immutable_fields(client, admin, doc):
    """`file_type` / `md5` / `version` / `doc_group_id` **不可改** ——
    要换内容请传新版本。静默忽略会让管理员以为改成功了。"""
    for field, value in (("md5", "0" * 32), ("version", 99),
                         ("doc_group_id", "other"), ("file_type", "docx")):
        r = await client.patch(f"/api/admin/documents/{doc['id']}",
                               json={field: value}, headers=_h(admin))
        assert r.status_code == 422, f"{field} 不该被接受：{r.text}"


async def test_patch_unknown_document_is_404(client, admin):
    r = await client.patch(f"/api/admin/documents/{uuid.uuid4().hex}",
                           json={"title": "x"}, headers=_h(admin))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


async def test_patch_empty_body_is_rejected(client, admin, doc):
    r = await client.patch(f"/api/admin/documents/{doc['id']}", json={},
                           headers=_h(admin))
    assert r.status_code == 400, r.text


# ============================================================
# 启用 / 停用 / 删除
# ============================================================

async def test_disable_enable_toggle(client, admin, doc):
    assert (await client.post(f"/api/admin/documents/{doc['id']}/disable",
                              headers=_h(admin))).status_code == 200
    assert not await _student_retrieves(doc["id"], doc["marker"])

    assert (await client.post(f"/api/admin/documents/{doc['id']}/enable",
                              headers=_h(admin))).status_code == 200
    assert await _student_retrieves(doc["id"], doc["marker"]), "重新启用后必须恢复可检索"


async def test_delete_removes_row_and_index_but_keeps_source_file(client, admin):
    """M4-D3：真删**三处**（Chroma chunk + BM25 条目 + PG 行），**源文件保留**。

    源文件不删是刻意的 —— 删了既不能回滚、也再拿不到原文。
    """
    title = f"删除测试_{uuid.uuid4().hex[:8]}"
    marker = _marker()
    doc_id = await ingest_text_doc(admin_id=admin["id"], title=title,
                                   text=_body(title, marker))

    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT source_path, normalized_text_path FROM documents WHERE id = $1",
            doc_id)
    source = Path(row["source_path"])

    chunk_ids = [r["chunk_id"] for r in vector.get_chunks(doc_id, limit=100000)]
    assert chunk_ids, "前置：应该有 chunk"
    async with db.tx() as conn:
        before = await conn.fetchval("SELECT count(*) FROM documents WHERE id = $1", doc_id)
    assert before == 1

    r = await client.delete(f"/api/admin/documents/{doc_id}", headers=_h(admin))
    assert r.status_code == 200, r.text

    async with db.tx() as conn:
        after = await conn.fetchval("SELECT count(*) FROM documents WHERE id = $1", doc_id)
    assert after == 0, "PG 行应被删除（不是软删）"
    assert vector.get_chunks(doc_id, limit=10) == [], "Chroma 里的 chunk 没删干净"
    assert not (_bm25_hits(chunk_ids, marker)), "BM25 里还留着孤儿条目"
    assert source.exists(), "源文件**不该**被删（可回溯）"


async def test_remove_from_index_without_chunk_ids_still_clears_bm25(admin):
    """★ M1 的 K-3：`remove_from_index` 按 `range(removed_chroma)` 推断 chunk_id ——
    Chroma 已经空掉时推出 `range(0)` = 空列表，**BM25 里的条目就留成孤儿**。

    这条路径是生产补偿删除用的，孤儿不会报错，只会让召回悄悄变差。
    """
    from app.services import index_service

    title = f"孤儿测试_{uuid.uuid4().hex[:8]}"
    marker = _marker()
    doc_id = await ingest_text_doc(admin_id=admin["id"], title=title,
                                   text=_body(title, marker))

    chunk_ids = [r["chunk_id"] for r in vector.get_chunks(doc_id, limit=100000)]
    assert chunk_ids
    assert _bm25_hits(chunk_ids, marker), "前置：BM25 里本来有这批 chunk"

    # 先把 Chroma 清空，制造「removed_chroma == 0 但 BM25 还在」的现场
    vector.delete_document(doc_id)

    index_service.remove_from_index(doc_id)          # 不传 chunk_ids

    assert not _bm25_hits(chunk_ids, marker), \
        "Chroma 已空时按条数推断 chunk_id，BM25 的条目全成了搜得到的孤儿"
    await purge_documents([title])


# ============================================================
# 评审 I-3：上传文件名必须净化（**预存在**，M4 评审发现）
# ============================================================

def test_upload_filename_cannot_escape_tmp_dir():
    """★ 文件名里的 `..` 能穿出 `data/tmp` —— 实测（Windows）：

        Path('data/tmp') / 'TASK_..\..\..\..\evil.txt'
            → D:\Knowledge_rag_system\evil.txt

    `{task_id}_` 前缀只挡住第一段，`..` 照样往上跳，而 `write_bytes`
    发生在**入库之前** —— 等于管理员上传即可往仓库任意路径写文件，
    且落在**下次启动会加载**的位置。

    只有 admin 能调，但「任意文件写」不该因为调用者可信就留着。
    """
    from app.core.config import repo_path
    from app.services import document_service

    tmp_root = Path(repo_path("data", "tmp")).resolve()
    evil = r"task123_..\..\..\..\evil.txt"
    for name in (evil, "../../evil.txt", r"..\..\evil.txt", "/etc/passwd",
                 r"C:\Windows\evil.txt"):
        p = Path(document_service._tmp_path("task123", name)).resolve()
        assert p.parent == tmp_root, (
            f"文件名 {name!r} 穿出了临时目录：{p}"
        )


def test_upload_filename_keeps_readable_suffix():
    """净化不能把扩展名一起砍掉 —— 后面靠扩展名判格式（file_type）。"""
    from app.services import document_service

    p = Path(document_service._tmp_path("task123", "桂林电子科技大学 学籍管理规定.pdf"))
    assert p.suffix == ".pdf", p
    assert p.parent.name == "tmp", p


# ============================================================
# 评审 M4-K6：删除失败要停在**看得见**的中间态
# ============================================================

async def test_delete_leaves_disabled_state_when_index_cleanup_fails(
        raw_client, admin, doc, monkeypatch):
    """★ 删索引失败时，PG 行不能停在 `active`。

    原实现是「先删索引 → 再删 PG 行」：索引删完、PG 那次 DELETE 若失败，
    行还在列表里显示 `active`、`chunk_count>0`，而内容其实已经检索不到了 ——
    **没有任何状态能标记这个不一致**，管理员看不出该重试。

    修法：先把行落到 `disabled`（管理端看得见、检索也停了），再删索引。
    失败就停在这个中间态，重试一次删除即可。
    """
    from app.services import document_service

    def boom(*_a, **_k):
        raise RuntimeError("模拟索引清理失败")

    monkeypatch.setattr(document_service.index_service, "remove_from_index", boom)

    r = await raw_client.delete(f"/api/admin/documents/{doc['id']}", headers=_h(admin))

    assert r.status_code >= 400, "删除失败必须报出来，不能假装成功"
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT status FROM documents WHERE id = $1", doc["id"])
    assert row is not None, "失败时行必须保留 —— 否则重试无从下手"
    assert row["status"] == "disabled", (
        f"失败后停在 {row['status']} —— 就是原实现那个「看不出该重试」的现场"
    )

    # ⚠️ **不断言「已经检索不到」**：检索期的 status 过滤读的是 **Chroma 元数据**，
    #    而失败场景恰恰是索引没清掉 —— 所以它**仍然搜得到**。
    #    这是跨存储做不到原子的固有代价，本轮不假装解决：能给的保证是
    #    「行还在、状态看得见、重试一次即可」，不是「立刻不可检索」。
    #    （写成断言会是一条永远为假的假断言 —— 我第一版就是这么写的。）
    async with db.tx() as conn:
        still = await conn.fetchval("SELECT 1 FROM documents WHERE id = $1", doc["id"])
    assert still, "重试路径必须成立：行还在，再删一次就能走完"


# ============================================================
# 评审 M4-K3：先写哪一侧，取决于这次改动是收紧还是放宽
# ============================================================

async def test_reindex_write_order_follows_direction(raw_client, admin, doc,
                                                     monkeypatch):
    """★ 原实现的注释声称「先 Chroma 后 PG，最坏情况是偏严」——
    那只在**收紧**方向成立。

    **放宽**（受限 → 公开）时先写 Chroma：Chroma 已经公开、PG 写失败 →
    检索对所有人放开，而管理端读 PG 还显示「受限」—— 管理员被自己看到的界面骗了。

    判据：让 Chroma 那次重写失败，看 **PG 有没有被写**，就能反推顺序。
    """
    from app.services import document_service

    def boom(*_a, **_k):
        raise RuntimeError("模拟向量库写入失败")

    # ---------- ① 收紧方向：public → restricted ----------
    monkeypatch.setattr(document_service.index_service, "rewrite_metadata", boom)
    r = await raw_client.patch(f"/api/admin/documents/{doc['id']}",
                               json={"visibility": "restricted",
                                     "visible_roles": ["admin"]},
                               headers=_h(admin))
    assert r.status_code >= 400, "Chroma 写失败必须报出来"
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT visibility FROM documents WHERE id = $1",
                                  doc["id"])
    assert row["visibility"] == "public", (
        "收紧方向必须先写 Chroma：Chroma 一失败就不该再动 PG"
    )

    # ---------- ② 放宽方向：restricted → public ----------
    # 先走**真实路径**把它改成受限（这一步不 patch）
    monkeypatch.undo()
    r = await raw_client.patch(f"/api/admin/documents/{doc['id']}",
                               json={"visibility": "restricted",
                                     "visible_roles": ["admin"]},
                               headers=_h(admin))
    assert r.status_code == 200, r.text
    assert not await _student_retrieves(doc["id"], doc["marker"]), "前置：已收紧"

    # 再让 Chroma 那次重写失败，改回公开
    monkeypatch.setattr(document_service.index_service, "rewrite_metadata", boom)
    r = await raw_client.patch(f"/api/admin/documents/{doc['id']}",
                               json={"visibility": "public", "visible_roles": []},
                               headers=_h(admin))
    assert r.status_code >= 400, "Chroma 写失败必须报出来"
    async with db.tx() as conn:
        row = await conn.fetchrow("SELECT visibility FROM documents WHERE id = $1",
                                  doc["id"])
    assert row["visibility"] == "public", (
        "放宽方向必须先写 PG：这样 Chroma 失败时最坏是「还收紧着」，"
        "而不是「界面说受限、学生搜得到」"
    )
