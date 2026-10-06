"""OTel trace_id 契约（§3.2.3.1）。

trace_id 直接采用 OTel 的 128-bit trace id（W3C Trace Context 格式），**不自造**。

| 项 | 约定 |
|---|---|
| 生成位置 | HTTP 入口中间件。请求头无 traceparent 则新建，有则沿用 |
| 传播格式 | W3C Trace Context（traceparent / tracestate） |
| 注入日志 | 每条日志必带 trace_id（32 位十六进制）与 span_id（16 位） |
| 跨 SSE 长连接 | 一次 SSE 流全程同一个 trace_id |
| 返回前端 | 响应头回传 traceparent |
| 后台任务 | ⚠️ 必须显式传递 —— 见 reattach() |

⚠️ trace_id 与 session_id 不是一回事，两者都要带：
     trace_id = 单次请求的，用于排障（「这一次为什么慢」）
     session_id = 多轮会话的，用于业务串联（「这个用户这一串问答」）

⚠️ span 属性必须同级脱敏（§3.2.3.2）：
     自动埋点（opentelemetry-instrumentation-langchain）默认会把 prompt 与模型返回
     写进 span 属性，后果是「日志干净了，Jaeger 里却摆着学号、姓名、成绩原文」。
     M5 接入自动埋点时必须实测确认内容捕获已关，且**不能只看文档**。

M0 只做 trace_id 的生成与传播；指标栈（Collector/Prometheus/Jaeger）在 M5 接入。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

_propagator = TraceContextTextMapPropagator()
_initialized = False

TRACEPARENT_HEADER = "traceparent"


def setup_tracing(service_name: str = "campus-rag") -> None:
    """初始化 TracerProvider，并在配了端点时挂上 OTLP 导出（§3.2.3.3）。

    ⚠️ **没配 `OTEL_EXPORTER_OTLP_ENDPOINT` 就不挂 exporter** —— 保持 M0 以来的
       行为不变：span 照建（`current_trace_id()`、日志里的 trace_id 都靠它），
       只是不往外发。跑测试、跑 CLI 时不会去连一个不存在的 Collector
       （BatchSpanProcessor 连不上会刷错误日志、拖慢退出）。

    采样策略：本项目流量低（校园内网），**默认全量采样，不设采样率**（§3.2.3.4）。
    将来若要改 head sampling，有一条硬约束：错误与降级的 trace 不能被采样丢弃 ——
    head sampling 做不到，需改尾采样或对错误强制 ALWAYS_ON。
    """
    global _initialized
    if _initialized:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(
            # 容器内直连 http://otel-collector:4317，明文（不加密）
            OTLPSpanExporter(endpoint=endpoint, insecure=True),
        ))
        logger.info("OTLP 导出已启用", extra={"event": "telemetry.exporter",
                                          "endpoint": endpoint})
    else:
        logger.info("未配置 OTEL_EXPORTER_OTLP_ENDPOINT —— span 只用于本地 trace_id，不外发",
                    extra={"event": "telemetry.exporter_disabled"})

    trace.set_tracer_provider(provider)
    _initialized = True


def get_tracer(name: str = "campus-rag"):
    return trace.get_tracer(name)


def extract_context(headers: dict[str, str]) -> otel_context.Context:
    """从请求头提取 W3C Trace Context；没有则返回空上下文（后续新建）。"""
    return _propagator.extract(carrier=dict(headers))


def inject_context(headers: dict[str, str]) -> dict[str, str]:
    """把当前上下文注入到 carrier，用于响应头回传 traceparent。"""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    headers.update(carrier)
    return headers


def current_trace_id() -> str:
    """32 位十六进制；无有效 span 时返回全 0（格式仍合法）。"""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else "0" * 32


def current_span_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else "0" * 16


def current_traceparent() -> str | None:
    headers: dict[str, str] = {}
    _propagator.inject(headers)
    return headers.get(TRACEPARENT_HEADER)


@contextmanager
def reattach(trace_id_hex: str, span_id_hex: str = "0" * 16) -> Iterator[None]:
    """在一个新任务里重新挂上某个 trace 的上下文。

    ⚠️ 后台任务不能靠 contextvars 隐式继承（§3.2.3.1）：
        上传 → 解析 → 嵌入是脱离请求生命周期的长任务，asyncio 任务可能在不同
        context 中运行，隐式继承会丢。
        做法：入口处把 trace_id 写进 ingestion_tasks 表，任务各阶段从表里取，
        再用本函数挂回来。

    同样适用于 SSE：Starlette 的 StreamingResponse 生成器可能跑在另一个 context 里，
    进生成器第一件事就是用本函数把 trace 挂回去。
    """
    span_id = int(span_id_hex, 16)
    parent = SpanContext(
        trace_id=int(trace_id_hex, 16),
        # span_id 全 0 在 OTel 里是「无效 span」，给个非零值保证上下文有效
        span_id=span_id if span_id else 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    token = otel_context.attach(set_span_in_context(NonRecordingSpan(parent)))
    try:
        yield
    finally:
        otel_context.detach(token)
