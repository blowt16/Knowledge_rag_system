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
from app.api import eval as eval_api
from app.api import stats as stats_api
from app.api import users as users_api
from app.core import observability, trace
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


async def _fail_stale_evals() -> None:
    """启动时把**上一进程遗留的**评测轮次标成 failed。

    ⚠️ 实测踩过：评测是进程内 asyncio 任务，服务重启 / 进程被杀时它跟着死，
       而 `eval_runs.status` 永远停在 `running`。而 `POST /eval/run` 有
       「同时只允许一轮」的保护 —— 于是**这一行僵死记录会把之后所有评测全部挡死**
       （409），且看起来像是"评测一直有人在跑"。
    """
    try:
        async with db.tx() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM eval_runs WHERE status IN ('pending','running')")
            if n:
                # ⚠️ 原因要同时写进 `error` **列** —— 那才是界面「失败原因」列读的
                #    （§3.4）。只写 `metrics.error` 的话，界面上是「失败」+ 空白原因。
                await conn.execute(
                    "UPDATE eval_runs SET status='failed', finished_at=now(), "
                    "error='stale: 上次进程退出时未结束', "
                    "metrics = COALESCE(metrics,'{}'::jsonb) || "
                    "'{\"error\":\"stale: 上次进程退出时未结束\"}'::jsonb "
                    "WHERE status IN ('pending','running')")
                logger.warning("清理了 %d 个上次未结束的评测轮次", n,
                               extra={"event": "eval.stale_cleaned", "count": n})
    except Exception:  # noqa: BLE001 —— 清理失败不该拦住启动
        logger.warning("清理遗留评测轮次失败", extra={"event": "eval.stale_clean_failed"})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_logging()
    # ⚠️ service.name 用**固定 ASCII 名**，不用 `cfg("app.name")`（那是中文显示名）。
    #    实测：中文 service.name 会以乱码出现在每一条标签上。
    observability.setup(service_name="campus-rag")
    await db.init_pool()
    await _fail_stale_evals()
    # 握住引用，避免任务被 GC 掉
    warmup = asyncio.create_task(asyncio.to_thread(_warm_reranker))
    logger.info("服务启动", extra={"event": "app.startup"})
    try:
        yield
    finally:
        warmup.cancel()
        await db.close_pool()
        logger.info("服务停止", extra={"event": "app.shutdown"})
        observability.shutdown()


app = FastAPI(
    title=cfg("app.name", "校园 RAG 检索问答系统"),
    version="0.1.0",
    lifespan=lifespan,
)

install_handlers(app)


def _is_hex(value: str, length: int) -> bool:
    """W3C trace id / span id 的合法性检查（长度 + 十六进制）。"""
    return len(value) == length and all(c in "0123456789abcdef" for c in value.lower())


def _incoming_trace_id(carrier: dict[str, str]) -> str | None:
    """入站 `traceparent` → trace id；**任何畸形形态一律当作「没有传入上下文」**。

    ⚠️ 这个 header **完全由客户端控制**，解析必须容错 —— 改造前用的是 OTel 官方
       propagator（`TraceContextTextMapPropagator`），畸形值静默忽略；
       等价行为是底线。手写解析后实测踩到**两条**客户端一行就能打出的 500：
         1. `traceparent: garbage` → 原稿 `.split("-")[1]` 直接 IndexError；
         2. `00-<32 位非十六进制>-…` → 值一路送进 Langfuse SDK，内部 `int(id, 16)`
            抛 ValueError。
       两条都在这里挡掉，不让客户端可控的字符串到达 SDK。

    W3C 格式：`00-<32hex trace-id>-<16hex span-id>-<2hex flags>`。
    版本 00 不允许额外字段；trace-id / span-id 全 0 规范判无效（OTel 同此判定）。
    版本 `ff` 保留、未知版本一律不解析 —— 本项目的契约就是 OTel 注入的 `00-` 形态。
    """
    parts = carrier.get("traceparent", "").split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if version != "00" or not _is_hex(flags, 2):
        return None
    if not _is_hex(trace_id, 32) or trace_id == "0" * 32:
        return None
    if not _is_hex(span_id, 16) or span_id == "0" * 16:
        return None
    return trace_id


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """trace_id 生成、传播与 HTTP 阶段日志（【方案 §5.3】）。

    每次请求都生成（或续接入站）32 位 trace_id；本地 trace 始终有值，
    Langfuse 启用时通过 `trace_context` 复用同一个 ID。
    """
    carrier = {k.lower(): v for k, v in request.headers.items()}
    # ⚠️ 入站 traceparent 必须**容错解析**（完全由客户端控制）：畸形一律当作
    #    「没有传入上下文」，绝不让一行请求头把请求打成 500。
    incoming = _incoming_trace_id(carrier)
    tid = incoming or trace.new_trace_id()

    with trace.begin_trace(tid), trace.trace_span("http", layer="http") as stage:
        stage.set(method=request.method, path=request.url.path)
        client = observability.get_client()

        if client is None:
            # 未启用：照常处理请求，本地 trace 仍然完整
            try:
                response = await call_next(request)
            except BaseException as e:  # noqa: BLE001
                stage.set(ok=False, error=type(e).__name__)
                raise
            route_path = getattr(request.scope.get("route"), "path",
                                 request.url.path)
            stage.set(path=route_path, status=response.status_code,
                      ok=response.status_code < 400)
            if response.status_code >= 500:
                stage.set_level(logging.ERROR)
            response.headers["x-trace-id"] = tid
            response.headers["traceparent"] = (
                f"00-{tid}-{trace.current_span_id()}-01")
            return response

        # ⚠️⚠️ **异常必须在这里吞掉、到 with 外面再重抛**：
        #     Langfuse 内部用的是 OTel 的 `start_as_current_span`，其 `use_span`
        #     默认 `record_exception=True`，而 SDK **不暴露关闭它的参数**。
        #     异常一旦穿过 with，`exception.message` 与整个栈就会发到云端。
        with client.start_as_current_observation(
            as_type="span",
            # 路由模板在 call_next 之后才可知；这里用固定名避免把 id 变成
            # 高基数 span 名，具体路由写进 metadata["http.route"]。
            name=f"{request.method} http",
            trace_context={"trace_id": tid},
            input=None, output=None,
        ) as span:
            pending: BaseException | None = None
            response = None
            try:
                response = await call_next(request)
            except BaseException as e:  # noqa: BLE001
                span.update(level="ERROR", status_message=type(e).__name__)
                pending = e

            if response is not None:
                route_path = getattr(request.scope.get("route"), "path",
                                     request.url.path)
                span.update(metadata={
                    "http.method": request.method,
                    "http.route": route_path,
                    "http.status_code": response.status_code,
                })
                # ★ 5xx 标 ERROR —— Metrics API 没有 HTTP 状态码维度，
                #    错误率面板靠 level 分组读（【方案 §8.3】）。
                if response.status_code >= 500:
                    span.update(level="ERROR",
                                status_message=f"HTTP {response.status_code}")
                    stage.set_level(logging.ERROR)
                stage.set(path=route_path, status=response.status_code,
                          ok=response.status_code < 400)

                # ⚠️ 两个 id 必须在 with **内**取：出了 with 观测已结束。
                # span_id 用**本地** http span：JSON 日志里的 span_id 也是它，
                # 这样从响应头能直接对到那一行日志；trace_id 仍是 Langfuse 的 join key。
                span_id = trace.current_span_id() or observability.current_span_id()
                response.headers["x-trace-id"] = tid
                response.headers["traceparent"] = f"00-{tid}-{span_id}-01"

        if pending is not None:
            raise pending          # 出了 with 才重抛 —— 见上面那条警告

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
            "trace_id": observability.current_trace_id()}


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
# 评测（§3.7.3）：起 run / 历史 / 单轮详情 / 消融对比表
app.include_router(eval_api.router)
