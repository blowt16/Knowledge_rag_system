"""本地全链路追踪：trace/span 上下文 + 阶段日志。

不依赖 Langfuse；`observability` 在它之上叠加云端追踪。
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)

_trace_id: ContextVar[str] = ContextVar("rag_trace_id", default="")
_trace_started: ContextVar[float] = ContextVar("rag_trace_started", default=0.0)
_span_stack: ContextVar[tuple["Span", ...]] = ContextVar("rag_span_stack", default=())

F = TypeVar("F", bound=Callable[..., Any])


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    name: str
    span_id: str
    parent_span_id: str
    depth: int
    started: float
    layer: str = "stage"
    fields: dict[str, Any] = field(default_factory=dict)

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)


def current_trace_id() -> str:
    return _trace_id.get()


def current_span_id() -> str:
    stack = _span_stack.get()
    return stack[-1].span_id if stack else ""


def current_stage() -> str:
    stack = _span_stack.get()
    return stack[-1].name if stack else ""


def current_layer() -> str:
    stack = _span_stack.get()
    return stack[-1].layer if stack else ""


def current_depth() -> int:
    return len(_span_stack.get())


def current_elapsed_ms() -> int:
    started = _trace_started.get()
    if not started:
        return 0
    return int((time.perf_counter() - started) * 1000)


@contextmanager
def begin_trace(trace_id: str | None = None) -> Iterator[str]:
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    started_token = _trace_started.set(time.perf_counter())
    try:
        yield tid
    finally:
        _trace_id.reset(token)
        _trace_started.reset(started_token)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _extra(event: str, span: "Span", *, ok: bool | None,
           ms: int | None = None, **overrides: Any) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "event": event,
        "trace_id": current_trace_id(),
        "span_id": span.span_id,
        "stage": span.name,
        "layer": span.layer,
        "depth": span.depth,
        "stage_id": span.span_id,
        "parent_stage_id": span.parent_span_id,
        "ok": ok,
    }
    extra.update(span.fields)
    extra.update(overrides)
    if ms is not None:
        extra["ms"] = ms
    return extra


@contextmanager
def trace_span(name: str, *, layer: str = "stage",
               level: int = logging.INFO, **fields: Any) -> Iterator[Span]:
    """记录一个阶段的开始/完成/失败；完成与失败各只打一行。"""
    parent_stack = _span_stack.get()
    span = Span(
        name=name,
        span_id=new_span_id(),
        parent_span_id=parent_stack[-1].span_id if parent_stack else "",
        depth=len(parent_stack),
        started=time.perf_counter(),
        layer=layer,
        fields=dict(fields),
    )
    token = _span_stack.set(parent_stack + (span,))
    try:
        if logger.isEnabledFor(logging.DEBUG):
            try:
                logger.debug("开始", extra=_extra("stage.start", span, ok=None))
            except Exception:  # noqa: BLE001 —— 日志不能拖垮业务
                pass
        try:
            yield span
        except Exception as e:
            try:
                logger.error("失败", exc_info=True,
                             extra=_extra("stage.failed", span, ok=False,
                                          ms=_ms(span.started),
                                          error=type(e).__name__))
            except Exception:  # noqa: BLE001
                pass
            raise
        else:
            try:
                logger.log(level, "完成",
                           extra=_extra("stage.done", span, ok=True,
                                        ms=_ms(span.started)))
            except Exception:  # noqa: BLE001
                pass
    finally:
        _span_stack.reset(token)


def stage(name: str | None = None, *, layer: str = "stage",
          level: int = logging.INFO, **static_fields: Any):
    """装饰器：sync / async 函数通用。"""
    def decorator(fn: F) -> F:
        span_name = name or fn.__name__
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any):
                with trace_span(span_name, layer=layer, level=level, **static_fields):
                    return await fn(*args, **kwargs)
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any):
            with trace_span(span_name, layer=layer, level=level, **static_fields):
                return fn(*args, **kwargs)
        return sync_wrapper  # type: ignore[return-value]

    return decorator
