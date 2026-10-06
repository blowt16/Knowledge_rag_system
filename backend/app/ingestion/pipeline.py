"""摄入主流程（§3.4.1 / §3.4.4）。

完整链路：

    上传（单文件 / zip 批量）
            ↓
      格式校验（扩展名 + 文件头嗅探）      ← 一处定义三处复用（E.4.3）
            ↓
      MD5 计算 → 判重（已存在 → duplicate，跳过）
            ↓
      【落盘 + 建行】保存原文件 → source_path
                     写 documents 行：status = indexing、**事务内分配 version**
                     —— 行必须先建：失败时才能按 document_id 补偿删除
            ↓
      内容解析（按格式分发；PDF 走两路分支）
            ↓
      图片提取落盘 → image_paths
            ↓
      【竖排检测 → 反转行序】  ← 已在加载层完成，且**必须在清洗之前**
            ↓
      文本清洗（控制字符 / 页眉页脚 / 目录行）
            ↓
      【规范化文本写出】→ documents.normalized_text_path
            ↓
      分块（500 字 / 50 重叠 / 中文标点切分）
            ↓
      元数据富化（章节、页码、bbox、chunk_id、图片路径）
            ↓
            批量向量化（20 chunk / 18000 字一批，指数退避重试）
                        ↓
            Chroma    BM25S    PostgreSQL（最后把 status 翻成 active）

⚠️ 「PostgreSQL 最后」指的是**翻状态**，**不是「最后才插行」** ——
   行必须先插，否则并发上传同一 doc_group_id 时事务内分配的 version 会撞车。

⚠️ 失败 → **补偿删除**：删 Chroma chunk → 删 BM25S 项 → 两张表的行**置 failed 且不删**。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import asyncpg

from app import db
from app.core.config import cfg, data_dir
from app.ingestion import versioning
from app.ingestion.chunker import ChunkSpan, chunk_text, make_chunk_id
from app.ingestion.clean import build_normalized_text
from app.ingestion.enrich import DocumentMeta, build_chunk_metadata
from app.ingestion.file_type import extension_of, is_supported
from app.ingestion.loaders import load as load_document
from app.ingestion.loaders.pdf import extract_chapter_marks
from app.services import index_service
from app.services.index_service import IndexEntry

logger = logging.getLogger(__name__)


@dataclass
class IngestRequest:
    source_path: Path          # 临时落地的上传文件
    filename: str
    uploader_id: str
    task_id: str
    batch_id: str
    trace_id: str
    title: str | None = None
    visibility: str = "public"
    visible_roles: list[str] = field(default_factory=list)
    effective_date: date | None = None


@dataclass
class IngestOutcome:
    status: str                # done | duplicate | failed
    document_id: str | None = None
    chunk_count: int = 0
    message: str = ""
    missing_pages: list[int] = field(default_factory=list)


async def _set_task(conn: asyncpg.Connection, task_id: str, *, status: str | None = None,
                    progress: int | None = None, message: str | None = None,
                    error: str | None = None, document_id: str | None = None) -> None:
    sets, args = [], []
    if status is not None:
        args.append(status); sets.append(f"status = ${len(args)}")
    if progress is not None:
        args.append(progress); sets.append(f"progress = ${len(args)}")
    if message is not None:
        args.append(message); sets.append(f"message = ${len(args)}")
    if error is not None:
        args.append(error); sets.append(f"error = ${len(args)}")
    if document_id is not None:
        args.append(document_id); sets.append(f"document_id = ${len(args)}")
    sets.append("updated_at = now()")
    args.append(task_id)
    await conn.execute(
        f"UPDATE ingestion_tasks SET {', '.join(sets)} WHERE id = ${len(args)}", *args
    )


def _data_path(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def ingest(req: IngestRequest) -> IngestOutcome:
    """摄入一个文件。异常不抛出 —— 一律转成 failed 结果并落库。"""
    source = req.source_path
    effective = req.effective_date or date.today()

    # ---- ① 格式校验（扩展名 + 内容嗅探）----------------------------
    ok, hint = is_supported(req.filename, path=source)
    if not ok:
        await _fail_task(req.task_id, hint)
        return IngestOutcome(status="failed", message=hint)

    # ---- ② MD5 与判重 ----------------------------------------------
    digest = md5_of(source)
    async with db.tx() as conn:
        await _set_task(conn, req.task_id, status="parsing", progress=5,
                        message="正在计算指纹")
        existing = await versioning.find_existing_by_md5(conn, digest)

    if existing is not None:
        async with db.tx() as conn:
            await _set_task(conn, req.task_id, status="duplicate", progress=100,
                            message=f"已存在相同文件（{existing['title']}），跳过")
        # duplicate 是**终态**（§3.7.2），不会被进度流拖住
        return IngestOutcome(status="duplicate",
                             document_id=existing["id"],
                             message="文件已存在，已跳过")

    # ---- ③ 落盘 + 建行（事务内分配 version）------------------------
    doc_id = uuid.uuid4().hex
    suffix = source.suffix or f".{extension_of(req.filename)}"
    stored = _data_path("uploads") / f"{doc_id}{suffix}"
    shutil.copy2(source, stored)

    title = (req.title or Path(req.filename).stem).strip() or Path(req.filename).stem

    try:
        async with db.tx() as conn:
            group_id, version = await versioning.resolve_group_and_version(conn, title)
            await conn.execute(
                """INSERT INTO documents
                   (id, doc_group_id, title, filename, file_type, md5, version,
                    effective_date, status, visibility, visible_roles,
                    source_path, uploader_id, chunk_count)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'indexing',$9,$10,$11,$12,0)""",
                doc_id, group_id, title, req.filename,
                extension_of(req.filename), digest, version, effective,
                req.visibility, req.visible_roles, str(stored), req.uploader_id,
            )
            await _set_task(conn, req.task_id, status="parsing", progress=15,
                            message="正在解析", document_id=doc_id)
    except asyncpg.UniqueViolationError:
        # UNIQUE(doc_group_id, version) 兜底（§3.3.1）
        message = "版本号冲突：同一文档组被并发上传，请重试"
        await _fail_task(req.task_id, message, doc_id)
        return IngestOutcome(status="failed", document_id=doc_id, message=message)

    # ---- ④ 解析 → 清洗 → 分块 → 富化（失败走补偿删除）--------------
    written_chunks = 0
    try:
        image_dir = _data_path("extracted_images", doc_id)
        result = load_document(stored, file_type=extension_of(req.filename),
                               image_dir=image_dir)

        async with db.tx() as conn:
            await _set_task(conn, req.task_id, status="chunking", progress=35,
                            message="正在清洗与分块")

        page_texts = [(p.page, p.text) for p in result.pages]
        normalized, slices = build_normalized_text(page_texts)
        if not normalized.strip():
            raise ValueError("解析后没有任何可用文本")

        normalized_path = _data_path("normalized") / f"{doc_id}.txt"
        normalized_path.write_text(normalized, encoding="utf-8")

        # 章节：PDF 走正则（E.4.2），docx/md 用结构化标题
        marks = result.heading_marks or extract_chapter_marks(normalized)
        if result.heading_marks and not marks:
            marks = extract_chapter_marks(normalized)

        boundaries: list[int] = []
        if len(result.pages) > 1:
            # PPTX：chunk 不跨幻灯片（§3.4.2）；其余格式也按页断开更稳
            boundaries = [s.char_start for s in slices[1:]]

        spans: list[ChunkSpan] = chunk_text(normalized, boundaries=boundaries)

        image_by_page = {p.page: p.image_paths for p in result.pages}
        page_boxes = [b for p in result.pages for b in p.bbox]

        meta = DocumentMeta(
            id=doc_id, doc_group_id=group_id, title=title, filename=req.filename,
            file_type=extension_of(req.filename), version=version,
            effective_date=effective, status="active",
            visibility=req.visibility, visible_roles=list(req.visible_roles or []),
        )

        entries: list[IndexEntry] = []
        for span in spans:
            chunk_meta = build_chunk_metadata(
                doc=meta, chunk=span, slices=slices, marks=marks,
                page_boxes=page_boxes, image_by_page=image_by_page,
            )
            entries.append(IndexEntry(
                chunk_id=make_chunk_id(doc_id, span.chunk_index),
                chunk_index=span.chunk_index,
                text=span.text,
                metadata=chunk_meta,
            ))

        # ---- ⑤ 写 Chroma + BM25S -----------------------------------
        async with db.tx() as conn:
            await _set_task(conn, req.task_id, status="embedding", progress=60,
                            message=f"正在向量化（{len(entries)} 个片段）")

        written_chunks = await index_service.build_index(entries)

        # ---- ⑥ 最后翻 active（「PostgreSQL 最后」= 翻状态）----------
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'active', chunk_count = $2, "
                "normalized_text_path = $3, updated_at = now() WHERE id = $1",
                doc_id, written_chunks, str(normalized_path),
            )
            await _set_task(conn, req.task_id, status="done", progress=100,
                            message=f"完成，共 {written_chunks} 个片段")

        logger.info("摄入完成", extra={
            "event": "ingest.done", "node": "ingest", "task_id": req.task_id,
        })
        return IngestOutcome(status="done", document_id=doc_id,
                             chunk_count=written_chunks,
                             missing_pages=result.missing_pages)

    except Exception as e:  # noqa: BLE001 —— 任何解析/嵌入失败都走补偿删除
        logger.exception("摄入失败，执行补偿删除", extra={
            "event": "ingest.failed", "node": "ingest", "task_id": req.task_id,
        })
        # 补偿删除：先索引、后 PG；且 PG 侧**只置状态，行永远保留**
        try:
            index_service.remove_from_index(doc_id)
        except Exception:  # noqa: BLE001
            logger.exception("补偿删除索引时再次失败（可能留下半删状态）",
                             extra={"event": "ingest.compensate_failed"})
        message = f"{type(e).__name__}: {e}"[:500]
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'failed', updated_at = now() WHERE id = $1",
                doc_id,
            )
            await _set_task(conn, req.task_id, status="failed", error=message,
                            document_id=doc_id)
        return IngestOutcome(status="failed", document_id=doc_id, message=message)


async def _fail_task(task_id: str, message: str, document_id: str | None = None) -> None:
    async with db.tx() as conn:
        await _set_task(conn, task_id, status="failed", error=message,
                        document_id=document_id)
