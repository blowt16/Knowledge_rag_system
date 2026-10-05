"""文档上传与进度（§3.7.2 / §3.7.3）。

**分层**（方案 §3.1）：`api/documents.py` 只做参数校验与响应封装，业务在这里。

**上传粒度**：一次 zip = 一个 `batch_id` + N 条 `ingestion_tasks`（每条对应包内一个文件）。
单文件上传时 `batch_id = task_id`、`tasks` 只有一项 —— **响应结构统一**，
前端不必写两套。

⚠️ M0 只实现单文件。zip 批量与 `upload/batch/{batch_id}/stream` 在 M4 补
   （只影响管理端体验，不阻塞端到端）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from app import db
from app.core.config import repo_path
from app.core.telemetry import reattach
from app.ingestion.pipeline import IngestRequest, ingest

logger = logging.getLogger(__name__)

# `counts` 的键**恰好是 ingestion_tasks.status 的 7 个取值**，一个不多一个不少
TASK_STATUSES = ["pending", "parsing", "chunking", "embedding",
                 "done", "duplicate", "failed"]

TERMINAL_STATUSES = {"done", "duplicate", "failed"}


def counts_for(status: str) -> dict[str, int]:
    """进度流 `counts` 载荷。键集必须与 `ingestion_tasks.status` 完全一致。"""
    return {k: (1 if k == status else 0) for k in TASK_STATUSES}


def _tmp_path(task_id: str, filename: str) -> Path:
    tmp_dir = repo_path("data", "tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / f"{task_id}_{filename}"


def normalize_roles(visible_roles: str, visibility: str) -> list[str]:
    """「受限」至少要有角色 —— 一个都不勾的话谁也看不到，且不会报错。"""
    roles = [r.strip() for r in (visible_roles or "").split(",") if r.strip()]
    if visibility == "restricted" and not roles:
        roles = ["admin"]
    return roles


def parse_effective_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def create_upload(
    *,
    filename: str,
    content: bytes,
    uploader_id: str,
    trace_id: str,
    title: str | None,
    visibility: str,
    visible_roles: str,
    effective_date: str | None,
) -> tuple[str, str]:
    """落地临时文件 + 建任务行。返回 (task_id, batch_id)。

    单文件时 batch_id = task_id，口径与 zip 批量统一。
    """
    task_id = uuid.uuid4().hex
    batch_id = task_id

    tmp_path = _tmp_path(task_id, filename)
    tmp_path.write_bytes(content)

    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (id, batch_id, file_name, status, progress, trace_id, uploader_id)
               VALUES ($1,$2,$3,'pending',0,$4,$5)""",
            task_id, batch_id, filename, trace_id, uploader_id,
        )

    return task_id, batch_id


def build_request(
    *,
    task_id: str,
    batch_id: str,
    filename: str,
    uploader_id: str,
    trace_id: str,
    title: str | None,
    visibility: str,
    visible_roles: str,
    effective_date: str | None,
) -> IngestRequest:
    return IngestRequest(
        source_path=_tmp_path(task_id, filename),
        filename=filename,
        uploader_id=uploader_id,
        task_id=task_id,
        batch_id=batch_id,
        trace_id=trace_id,
        title=title,
        visibility=visibility if visibility in ("public", "restricted") else "public",
        visible_roles=normalize_roles(visible_roles, visibility),
        effective_date=parse_effective_date(effective_date),
    )


def schedule_ingest(req: IngestRequest, *, trace_id: str, span_id: str) -> None:
    """把摄入丢到后台跑。

    ⚠️ 后台任务不能靠 contextvars 隐式继承 —— trace_id 已落库，
       这里显式挂回来（§3.2.3.1）。

    ⚠️ M0 用进程内 asyncio 任务；M4 换成由 `ingestion_tasks` 表驱动的 worker。
       进度流已按表实现，届时接口不变。
    """

    async def _run() -> None:
        with reattach(trace_id, span_id):
            try:
                await ingest(req)
            finally:
                req.source_path.unlink(missing_ok=True)

    asyncio.create_task(_run())


async def task_exists(task_id: str) -> bool:
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM ingestion_tasks WHERE id = $1", task_id)
    return row is not None


async def stream_progress(task_id: str, *, interval: float = 0.5) -> AsyncIterator[str]:
    """单文件上传进度 SSE。

    ⚠️ 这是与 chat 流**互相独立的一套协议**，不要复用 chat 的事件语义：
       chat 的 `error` 是**终止事件**，而这里单个文件失败**不影响整包继续**。

    ⚠️ 流的结束条件：整包不再有进行中的状态。`duplicate` 与 `failed` **都算终态**，
       不会被它们拖住（否则一次失败会让流永远挂着）。
    """
    last_status: str | None = None
    while True:
        async with db.tx() as conn:
            task = await conn.fetchrow(
                """SELECT status, progress, message FROM ingestion_tasks
                    WHERE id = $1""",
                task_id,
            )
        if task is None:
            break

        if task["status"] != last_status:
            last_status = task["status"]
            payload = {
                "done": 1 if status_is_done(task["status"]) else 0,
                # ⚠️ total 是**包内文件数**；单文件时恒为 1，
                #    进度百分比会长时间停在 0% 或 100% ——
                #    所以前端**必须**同时显示 current_file 作为兜底
                "total": 1,
                "current_file": task["message"] or "",
                "counts": counts_for(task["status"]),
            }
            yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if task["status"] in TERMINAL_STATUSES:
            yield ("event: done\ndata: "
                   + json.dumps({"batch_id": task_id,
                                 "counts": counts_for(task["status"])},
                                ensure_ascii=False) + "\n\n")
            break

        await asyncio.sleep(interval)


def status_is_done(status: str) -> bool:
    return status == "done"
