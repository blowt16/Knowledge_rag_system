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

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.api import admin as admin_api
from app.api import auth as auth_api
from app.api import chat as chat_api
from app.api import conversations as conversations_api
from app.api import document_access as document_access_api
from app.api import documents as documents_api
from app.api import stats as stats_api
from app.api import users as users_api
from app.core import metrics
from app.core import telemetry
from app.core.config import cfg
from app.core.exceptions import install_handlers
from app.core.logging import install as install_logging
from app.retrieval import reranker

logger = logging.getLogger(__name__)


def _warm_reranker() -> None:
    """后台预热精排模型（§3.5.3 节点 7）。

    ⚠️ 冷启动实测 **23–80 秒**（首次 79.5s / 页缓存热 23s），加载后推理只要 0.4–3s。
       不预热的话第一个知识型提问要等一分多钟 —— M0 结束时最大的体验问题。

    放**后台线程**而不是启动时 await：服务秒起、`/health` 立即可用，
    不必为了一个可选环节把启动堵住一分钟；若第一个提问恰好撞上预热，
    它会在这把锁上等预热加载完（`reranker._load_lock`），不会加载两次。

    失败不让启动失败 —— 精排本来就有降级链，记一行日志即可。
    """
    started = time.perf_counter()
    ok = reranker.preload()
    logger.info(
        "精排模型预热%s",
        "完成" if ok else "失败（后续请求走降级链）",
        extra={"event": "rerank.preloaded", "node": "rerank", "ok": ok,
               "ms": int((time.perf_counter() - started) * 1000)},
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_logging()
    # ⚠️ service.name 用**固定 ASCII 名**，不用 `cfg("app.name")`（那是中文显示名）。
    #    实测：中文 service.name 经 collector 的 prometheus exporter 出来是
    #    `exported_job="校园 RAG 检索问答系统"` 的**乱码**（编码坏掉），
    #    而它会出现在每一条指标的标签上。
    telemetry.setup_tracing(service_name="campus-rag")
    # 指标（§3.2.3.3）—— 与 tracing 同一开关：没配 OTEL_EXPORTER_OTLP_ENDPOINT 就不导出
    metrics.setup_metrics(service_name="campus-rag")
    await db.init_pool()
    # 握住引用，避免任务被 GC 掉
    warmup = asyncio.create_task(asyncio.to_thread(_warm_reranker))
    logger.info("服务启动", extra={"event": "app.startup"})
    try:
        yield
    finally:
        warmup.cancel()
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
app.include_router(conversations_api.router)
app.include_router(documents_api.router)
# User 端原文访问（/file、/text、/images/{name}）—— 走 filters.py 同一套 ACL
app.include_router(document_access_api.router)
# 管理端（文档 CRUD / 版本 / 分块预览）—— 注册在 documents 之后，
# 同前缀下段数不同、不冲突，但顺序上更稳
app.include_router(admin_api.router)
app.include_router(users_api.router)
app.include_router(stats_api.router)
