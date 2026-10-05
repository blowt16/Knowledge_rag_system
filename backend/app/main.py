"""FastAPI 入口。

⚠️ 启动方式（单 worker 是硬约束，§3.2.4 / 附录 G）：
       cd backend && uv run uvicorn app.main:app --workers 1 --reload

   `--workers 1` 不能省 —— 理由已从「进程内锁」改为「**Chroma 内嵌**」：
   PersistentClient 直接打开 data/chromadb/chroma.sqlite3，多 worker 会争抢同一个文件；
   BM25S 的磁盘索引同理。换了 PG 之后「多 worker」看着可行了，但 Chroma 仍然不支持。

⚠️ 本文件**没有** `app.mount("/images", StaticFiles(...))`（§3.7.2）：
   旧项目有这个挂载，等于把受限文档的图片目录直接对外裸露。
   图片改由带鉴权的 `GET /api/documents/{id}/images/{name}` 提供，
   它走 filters.py 的同一套行级 ACL，返回 5 分钟过期的签名 URL。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.api import auth as auth_api
from app.api import chat as chat_api
from app.api import documents as documents_api
from app.core import telemetry
from app.core.config import cfg
from app.core.exceptions import install_handlers
from app.core.logging import install as install_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_logging()
    telemetry.setup_tracing(service_name=cfg("app.name", "campus-rag"))
    await db.init_pool()
    logger.info("服务启动", extra={"event": "app.startup"})
    try:
        yield
    finally:
        await db.close_pool()
        logger.info("服务停止", extra={"event": "app.shutdown"})


app = FastAPI(
    title=cfg("app.name", "校园 RAG 检索问答系统"),
    version="0.1.0",
    lifespan=lifespan,
)

install_handlers(app)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """trace_id 生成与传播（§3.2.3.1）。

    请求头无 traceparent 则新建，有则沿用（便于将来接网关或前端串联）。
    """
    carrier = {k.lower(): v for k, v in request.headers.items()}
    ctx = telemetry.extract_context(carrier)

    tracer = telemetry.get_tracer("http")
    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}", context=ctx
    ) as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.route", request.url.path)
        response = await call_next(request)
        span.set_attribute("http.status_code", response.status_code)
        # 响应头回传 traceparent —— 用户报障时可直接定位
        traceparent = telemetry.current_traceparent()
        if traceparent:
            response.headers["traceparent"] = traceparent
        response.headers["x-trace-id"] = telemetry.current_trace_id()
        return response


@app.get("/health", tags=["meta"])
async def health():
    """健康检查。PG 不通时返回 503，便于启动脚本判断。"""
    try:
        async with db.tx() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": f"unavailable: {type(e).__name__}"},
        )
    return {"status": "ok", "database": "ok",
            "trace_id": telemetry.current_trace_id()}


app.include_router(auth_api.router)
app.include_router(chat_api.router)
app.include_router(documents_api.router)
