"""摄入流水线（§3.4.1 / §3.4.4 / §3.3.1）。

对应施工计划 M0-8 的测试点：
  ① 同一文件传两次 → 第二次 `duplicate`
  ② 同 title 传新版 → version=2，旧版仍 active
  ③ 故意让嵌入失败 → Chroma 无残留、两表行都在且 failed
  ④ **失败上传后再传新版，version 不撞车**（§3.4.3 那条最难排查的错）

⚠️ 会真实调用嵌入 API（很便宜）与写本地 Chroma/BM25 索引。
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

from app import db
from app.core.config import repo_path
from app.ingestion.pipeline import IngestRequest, ingest
from app.retrieval import bm25, vector
from tests.support import purge_documents

CORPUS = repo_path("corpus", "guet")
SAMPLE = next(iter(sorted(CORPUS.glob("09_*.pdf"))), None) if CORPUS.exists() else None

pytestmark = pytest.mark.skipif(SAMPLE is None, reason="语料不存在")


@pytest_asyncio.fixture
async def admin_id():
    user_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, role, token_version) "
            "VALUES ($1, $2, 'x', 'admin', 0)",
            user_id, f"ing_{user_id[:8]}",
        )
    yield user_id
    # 先清掉引用该用户的行，否则 FK 会拦住 users 的删除
    async with db.tx() as conn:
        await conn.execute("DELETE FROM ingestion_tasks WHERE uploader_id = $1", user_id)
        await conn.execute("DELETE FROM documents WHERE uploader_id = $1", user_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest_asyncio.fixture
async def cleanup_docs():
    """记录测试产生的文档标题，用完清掉（PG 行 + Chroma + BM25S）。"""
    titles: list[str] = []
    yield titles
    await purge_documents(titles)


def _unique_copy(tag: str) -> Path:
    """复制样本并追加唯一字节，得到唯一 MD5。

    ⚠️ 必须这么做：**MD5 判重是全局的（不看 title）** ——
       同一份文件内容换个标题上传，仍会被判 `duplicate` 跳过（这是 §3.3.1 的既定行为）。
       若各用例共用同一份样本，第一个用例入库后，其余用例全会拿到 `duplicate`，
       表现为「测试莫名失败」，且**与运行顺序、以及库里是否已有该 MD5 有关**。
    """
    out = Path(repo_path("data", "tmp")) / f"{tag}_{uuid.uuid4().hex}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(SAMPLE.read_bytes() + f"\n%{uuid.uuid4().hex}".encode())
    return out


def _req(admin_id: str, title: str, tmp_name: str | None = None) -> IngestRequest:
    task_id = uuid.uuid4().hex
    return IngestRequest(
        source_path=_unique_copy(title),
        filename=tmp_name or SAMPLE.name,
        uploader_id=admin_id,
        task_id=task_id,
        batch_id=task_id,
        trace_id="0" * 32,
        title=title,
        visibility="public",
        visible_roles=[],
        effective_date=date(2020, 1, 1),
    )


async def _seed_task(req: IngestRequest) -> None:
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (id, batch_id, file_name, status, progress, uploader_id, trace_id)
               VALUES ($1, $2, $3, 'pending', 0, $4, $5)""",
            req.task_id, req.batch_id, req.filename, req.uploader_id, req.trace_id,
        )


# ---- 正常路径 ----------------------------------------------------------

async def test_ingest_produces_chunks_in_both_indexes(admin_id, cleanup_docs):
    title = f"测试文档_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    req = _req(admin_id, title)
    await _seed_task(req)
    outcome = await ingest(req)

    assert outcome.status == "done", outcome.message
    assert outcome.chunk_count > 0

    rows = vector.get_chunks(outcome.document_id, limit=1000)
    assert len(rows) == outcome.chunk_count
    assert bm25.count() >= outcome.chunk_count

    # metadata 关键字段齐备（§3.3.2）
    first = rows[0]["metadata"]
    for key in ("document_id", "doc_group_id", "version", "status", "effective_date",
                "visibility", "vis_admin", "vis_staff", "vis_student",
                "chunk_id", "chunk_index", "char_start", "char_end", "page"):
        assert key in first, f"metadata 缺字段 {key}"
    assert first["status"] == "active"
    assert first["vis_admin"] is False and first["vis_student"] is False


async def test_duplicate_upload_is_skipped(admin_id, cleanup_docs):
    """★ 测试点①：同一文件（MD5 相同）再传 → duplicate，不重复入库。

    本用例是唯一**刻意复用同一份内容**的：判重的前提就是内容相同。
    """
    title = f"判重测试_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    shared = _unique_copy("dup")

    first = _req(admin_id, title)
    first.source_path = shared
    await _seed_task(first)
    assert (await ingest(first)).status == "done"

    second = _req(admin_id, title)
    second.source_path = shared          # 同一份内容 —— 这次才该判重复
    await _seed_task(second)
    outcome = await ingest(second)
    assert outcome.status == "duplicate", outcome.message


async def test_new_version_increments_and_old_stays_active(admin_id, cleanup_docs):
    """★ 测试点②：同 title 再传 → version=2，旧版**状态不变**（仍是 active）。

    这正是「检索期解析当前生效版本」的前提 ——
    写入时翻转状态会让 8 月提前上传的 9 月新规造成**政策真空期**。
    """
    title = f"版本测试_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    # 第一次
    req1 = _req(admin_id, title)
    await _seed_task(req1)
    out1 = await ingest(req1)
    assert out1.status == "done"

    # 改一个字节，绕过 MD5 判重
    tampered = Path(repo_path("data", "tmp")) / f"{uuid.uuid4().hex}.pdf"
    tampered.parent.mkdir(parents=True, exist_ok=True)
    tampered.write_bytes(SAMPLE.read_bytes() + b"\n%tamper")

    req2 = _req(admin_id, title, tmp_name=SAMPLE.name)
    req2.source_path = tampered
    await _seed_task(req2)
    out2 = await ingest(req2)
    assert out2.status == "done", out2.message

    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT id, version, status FROM documents WHERE title = $1 ORDER BY version",
            title,
        )
    assert [r["version"] for r in rows] == [1, 2]
    assert all(r["status"] == "active" for r in rows), "旧版状态不应被改动"


# ---- 失败与补偿删除 ----------------------------------------------------

async def test_embedding_failure_triggers_compensation(admin_id, cleanup_docs, monkeypatch):
    """★ 测试点③：嵌入失败 → Chroma 无残留、两张表的行**都在**且置 failed。

    「行永远保留」是关键：删了行，管理端就分不清「没传过」和「传失败了」，
    而这两种情况的处置完全不同（前者要传新版，后者要排查失败原因）。
    """
    title = f"失败测试_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    async def _boom(*_a, **_kw):
        raise RuntimeError("模拟嵌入失败")

    monkeypatch.setattr("app.services.index_service.embed_texts", _boom)

    req = _req(admin_id, title)
    await _seed_task(req)
    outcome = await ingest(req)

    assert outcome.status == "failed"
    assert outcome.document_id

    # Chroma 无残留
    assert vector.get_chunks(outcome.document_id, limit=10) == []

    async with db.tx() as conn:
        doc = await conn.fetchrow("SELECT status FROM documents WHERE id = $1",
                                  outcome.document_id)
        task = await conn.fetchrow("SELECT status, error FROM ingestion_tasks WHERE id = $1",
                                   req.task_id)
    assert doc is not None, "documents 行被删了 —— 必须保留并置 failed"
    assert doc["status"] == "failed"
    assert task is not None, "ingestion_tasks 行被删了 —— 失败队列要靠它"
    assert task["status"] == "failed"
    assert "模拟嵌入失败" in (task["error"] or "")


async def test_version_not_reused_after_failure(admin_id, cleanup_docs, monkeypatch):
    """★ 测试点④：失败行**占着版本号**，下次上传不能分到同一个 version。

    §3.4.3 点名这是「平时不出现、出过一次失败上传之后才开始出现」的错 ——
    只按 active 行取 max(version) 就会撞 `UNIQUE(doc_group_id, version)`。
    本测试锁死这个回归。
    """
    title = f"版本占用_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    async def _boom(*_a, **_kw):
        raise RuntimeError("模拟嵌入失败")

    monkeypatch.setattr("app.services.index_service.embed_texts", _boom)

    req1 = _req(admin_id, title)
    await _seed_task(req1)
    out1 = await ingest(req1)
    assert out1.status == "failed"

    # 恢复嵌入，再传一次
    monkeypatch.undo()

    req2 = _req(admin_id, title)
    await _seed_task(req2)
    out2 = await ingest(req2)
    assert out2.status == "done", f"失败后重传撞车了：{out2.message}"

    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT version, status FROM documents WHERE title = $1 ORDER BY version",
            title,
        )
    versions = [r["version"] for r in rows]
    assert versions == [1, 2], f"版本号应连续且不复用，实得 {versions}"
    assert rows[0]["status"] == "failed"
    assert rows[1]["status"] == "active"


async def test_cleanup_leaves_both_indexes_unchanged(admin_id, cleanup_docs):
    """★ 回归锁：用例收尾必须把 Chroma **和 BM25S** 都清干净。

    清理漏一处 → 每跑一次测试就往真实索引里漏一批死条目，
    而平时完全看不出来（不报错、只让召回变差）。
    """
    before_vector = vector.count()
    before_bm25 = bm25.count()

    title = f"清理回归_{uuid.uuid4().hex[:8]}"
    cleanup_docs.append(title)

    req = _req(admin_id, title)
    await _seed_task(req)
    outcome = await ingest(req)
    assert outcome.status == "done", outcome.message
    assert bm25.count() > before_bm25, "入库没写进 BM25，本用例就没有意义"

    await purge_documents([title])

    assert vector.count() == before_vector, "Chroma 有残留"
    assert bm25.count() == before_bm25, "BM25 有残留死条目"


async def test_bad_format_rejected_before_any_row(admin_id):
    """格式不符应在建行**之前**就被拦下（不给库里留垃圾行）。"""
    bogus = Path(repo_path("data", "tmp")) / f"{uuid.uuid4().hex}.pdf"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)   # 其实是 PNG

    task_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks (id, batch_id, file_name, status, uploader_id)
               VALUES ($1,$1,$2,'pending',$3)""",
            task_id, "fake.pdf", admin_id,
        )

    req = IngestRequest(
        source_path=bogus, filename="fake.pdf", uploader_id=admin_id,
        task_id=task_id, batch_id=task_id, trace_id="0" * 32,
        title=f"格式测试_{uuid.uuid4().hex[:8]}",
    )
    outcome = await ingest(req)
    assert outcome.status == "failed"
    assert "PNG" in outcome.message, f"提示语应说人话，实得：{outcome.message}"

    async with db.tx() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE title = $1", req.title)
        await conn.execute("DELETE FROM ingestion_tasks WHERE id = $1", task_id)
    assert count == 0, "格式不符的文件不该在 documents 里留行"
