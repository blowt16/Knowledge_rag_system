# 全链路日志追踪 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为校园 RAG 后端补齐本地全链路追踪：每次请求/摄入/评测都能在控制台按阶段看到耗时、数量和降级信息，同时保留 `logs/app.jsonl` 的机器可读日志与 Langfuse 云端追踪。

**Architecture:** 新增 `core/trace.py`，用 ContextVar 维护本地 trace/span 栈；`trace_span` 上下文管理器在阶段完成时打一行日志。控制台改用 `ConsoleFormatter` 输出人读文本，文件 handler 继续 `JsonFormatter`。Langfuse 仍作为可选后端，中间件把本地 trace_id 通过 `trace_context` 传给 Langfuse，保证日志、响应头、平台三处一致。

**Tech Stack:** Python 3.13、FastAPI、stdlib logging、contextvars、Langfuse（可选）、pytest。

**Spec:** `docs/superpowers/specs/2026-10-19-log-tracing-design.md`

## Global Constraints

- 不新增第三方依赖；继续使用 stdlib `logging`。
- 不记录 query / answer / prompt / 检索正文；只记录数量、长度、耗时、分数、枚举。
- 现有 JSON 日志字段只增不改；`logs/app.jsonl` 必须保持一行一个合法 JSON。
- 单 worker 部署约束不变。
- 追踪组件自身故障不能影响业务；所有日志调用要能兜底。
- 控制台默认 INFO 只显示主流程；`LOG_LEVEL=DEBUG` 才展开子阶段。
- 每次提交只包含本次任务相关文件，不提交日志/临时文件。

## Review Focus

- **并发请求串 trace**：两个并发请求不能互相污染 trace_id / span 栈；测试两个 `begin_trace` 任务并发运行。
- **SSE/后台任务丢上下文**：`StreamingResponse` 生成器、`asyncio.create_task` 后台任务必须显式续接 trace；测试请求头 trace_id 与生成器内日志一致。
- **Langfuse 开关一致性**：关闭时本地 trace 仍非 0；开启时日志/响应头/Langfuse trace_id 三者一致；两种状态都测。
- **正文泄露**：用哨兵字符串（如 `SECRET_QUERY_9f3a`）跑一次完整问答/LLM 调用，断言控制台和 JSON 日志里都不出现。
- **追踪故障不打断业务**：格式器遇到不可序列化值、`trace_span` 内部日志抛错时，业务流程仍成功。
- **LLM 流提前关闭**：消费者提前 `aclose()` 时 LLM span 仍要结束，不能留下悬挂阶段。
- **uvicorn 重复日志**：统一 handler 后访问日志不能出现两份；P3 手工核对。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `backend/app/core/trace.py`（新增） | 本地 trace/span 上下文、`trace_span`、`stage` 装饰器 |
| `backend/app/core/logging.py` | ConsoleFormatter、JSON 新增字段、handler 装配、uvicorn 统一 |
| `backend/app/core/observability.py` | Langfuse 与本地 trace_id 对齐 |
| `backend/app/main.py` | HTTP 中间件接入 trace、HTTP 完成日志 |
| `backend/app/graph/builder.py` | 图节点统一计时/日志，不依赖 Langfuse |
| `backend/app/graph/node_io.py` | 新增 `node_log_fields`（只提取标量字段） |
| `backend/app/core/llm.py` | LLM 阶段日志、TTFT、tokens |
| `backend/app/retrieval/{embedding,search,fusion,reranker}.py` | 检索子阶段 span |
| `backend/app/services/chat_service.py` | chat 首字/完成/断连汇总 |
| `backend/app/services/context_service.py` | 上下文压缩 span、compaction 节点上下文 |
| `backend/app/ingestion/pipeline.py` | 摄入阶段 span、trace 入口 |
| `backend/app/services/index_service.py` | 嵌入/写索引 span |
| `backend/app/services/document_service.py` | 后台摄入续接 trace |
| `backend/app/eval/runner.py` | 评测 run/case span |
| `backend/app/config/app.yaml` | 控制台级别/格式/颜色配置 |
| `backend/tests/unit/test_trace_context.py`（新增） | trace 核心测试 |
| `backend/tests/unit/test_console_formatter.py`（新增） | 控制台格式测试 |
| `backend/tests/unit/test_node_log_fields.py`（新增） | 节点字段测试 |
| `backend/tests/unit/test_http_trace.py`（新增） | 中间件 trace 测试 |
| `backend/tests/unit/test_llm_trace.py`（新增） | LLM 日志测试 |
| `backend/tests/unit/test_retrieval_trace.py`（新增） | 检索子阶段测试 |
| `backend/tests/integration/test_chat_summary_log.py`（新增） | chat 汇总日志 |
| `backend/tests/unit/test_logging_trace_id.py`（更新） | trace_id 契约更新 |
| `docs/日志追踪使用说明.md`（新增） | 开发者使用说明 |

---

## Task 1: trace 核心

**Files:**
- Create: `backend/app/core/trace.py`
- Test: `backend/tests/unit/test_trace_context.py`

**Interfaces:**
- Produces: `new_trace_id() -> str`, `new_span_id() -> str`, `begin_trace(trace_id: str | None = None) -> ContextManager[str]`, `trace_span(name, *, layer="stage", level=logging.INFO, **fields) -> ContextManager[Span]`, `stage(name, **static_fields)` 装饰器, `current_trace_id()`, `current_span_id()`, `current_stage()`, `current_layer()`, `current_depth()`, `current_elapsed_ms()`, `Span.set(**fields)`.

- [ ] **Step 1: 写失败测试**

```python
"""本地 trace/span 上下文。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.core import trace


def test_new_ids_are_hex():
    tid = trace.new_trace_id()
    sid = trace.new_span_id()
    assert len(tid) == 32 and int(tid, 16) >= 0
    assert len(sid) == 16 and int(sid, 16) >= 0


def test_begin_trace_sets_and_resets():
    assert trace.current_trace_id() == ""
    with trace.begin_trace("a" * 32) as tid:
        assert tid == "a" * 32
        assert trace.current_trace_id() == "a" * 32
    assert trace.current_trace_id() == ""


def test_nested_spans_parent_and_depth():
    with trace.begin_trace():
        assert trace.current_depth() == 0
        with trace.trace_span("outer") as outer:
            assert outer.depth == 0
            assert trace.current_stage() == "outer"
            with trace.trace_span("inner") as inner:
                assert inner.depth == 1
                assert inner.parent_span_id == outer.span_id
                assert trace.current_stage() == "inner"
            assert trace.current_stage() == "outer"
        assert trace.current_stage() == ""


async def test_trace_propagates_to_created_task():
    with trace.begin_trace("b" * 32):
        async def child() -> str:
            return trace.current_trace_id()

        assert await asyncio.create_task(child()) == "b" * 32


def test_span_logs_done_with_fields(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        with trace.begin_trace():
            with trace.trace_span("retrieve", layer="stage", candidates=17):
                pass

    done = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"]
    assert done, "没有 stage.done 日志"
    rec = done[-1]
    assert rec.stage == "retrieve"
    assert rec.layer == "stage"
    assert rec.ok is True
    assert rec.ms >= 0
    assert rec.candidates == 17


def test_span_logs_failure_and_reraises(caplog):
    with caplog.at_level(logging.ERROR, logger="app.core.trace"):
        with trace.begin_trace():
            with pytest.raises(RuntimeError):
                with trace.trace_span("bad"):
                    raise RuntimeError("boom")

    failed = [r for r in caplog.records if getattr(r, "event", "") == "stage.failed"]
    assert failed, "没有 stage.failed 日志"
    assert failed[-1].stage == "bad"
    assert failed[-1].error == "RuntimeError"
    assert failed[-1].ok is False


def test_span_set_updates_final_fields(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        with trace.begin_trace():
            with trace.trace_span("retrieve") as span:
                span.set(candidates=5, degraded=None)

    rec = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"][-1]
    assert rec.candidates == 5
    assert rec.degraded is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_trace_context.py -q`
Expected: FAIL，`ModuleNotFoundError: app.core.trace`

- [ ] **Step 3: 实现 `trace.py`**

```python
"""本地全链路追踪：trace/span 上下文 + 阶段日志。

不依赖 Langfuse；observability 在它之上叠加云端追踪。
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


def _extra(event: str, span: Span, *, ok: bool | None,
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_trace_context.py -q`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/trace.py backend/tests/unit/test_trace_context.py
git commit -m "[feat] 新增本地 trace/span 上下文"
```

---

## Task 2: 控制台格式器与配置

**Files:**
- Modify: `backend/app/core/logging.py:1-141`
- Modify: `backend/app/config/app.yaml:406-414`
- Test: `backend/tests/unit/test_console_formatter.py`

**Interfaces:**
- Consumes: `trace.current_trace_id()`, `trace.current_stage()`, `trace.current_layer()`, `trace.current_depth()`, `trace.current_span_id()`.
- Produces: `ConsoleFormatter(use_color: bool = False)`, `_color_enabled() -> bool`, updated `JsonFormatter` with top-level `stage` / `depth` / `stage_id` / `parent_stage_id`.

- [ ] **Step 1: 写失败测试**

```python
"""控制台文本格式器与 JSON 新增字段。"""
from __future__ import annotations

import json
import logging

from app.core.logging import ConsoleFormatter, JsonFormatter


def _record(msg: str = "完成", **extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_console_line_contains_trace_stage_duration_and_fields():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(
        trace_id="a1b2c3d4" + "0" * 24, stage="retrieve", depth=1,
        event="stage.done", ms=2717, ok=True, candidates=17,
    ))
    assert "[a1b2c3d4]" in out
    assert "retrieve" in out
    assert "2.72s" in out
    assert "candidates=17" in out
    assert "{" not in out


def test_console_truncates_and_strips_newlines():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(
        trace_id="a" * 32, stage="parse", depth=0,
        event="stage.done", ms=5, ok=True,
        detail="x" * 300 + "\nsecond",
    ))
    assert "\n" not in out
    assert "x" * 121 not in out
    assert "detail=" in out


def test_console_no_color_when_disabled():
    fmt = ConsoleFormatter(use_color=False)
    out = fmt.format(_record(trace_id="a" * 32, stage="route",
                             event="stage.done", ms=8, ok=True))
    assert "\033[" not in out


def test_json_formatter_adds_stage_fields(monkeypatch):
    from app.core import observability
    monkeypatch.setattr(observability, "current_trace_id", lambda: "a" * 32)
    monkeypatch.setattr(observability, "current_span_id", lambda: "b" * 16)

    payload = json.loads(JsonFormatter().format(_record(
        stage="retrieve", depth=1, stage_id="c" * 16,
        parent_stage_id="d" * 16, event="stage.done", ms=12,
    )))
    assert payload["stage"] == "retrieve"
    assert payload["depth"] == 1
    assert payload["stage_id"] == "c" * 16
    assert payload["parent_stage_id"] == "d" * 16
    assert payload["extra"]["ms"] == 12


def test_json_formatter_still_scrubs_extra():
    payload = json.loads(JsonFormatter().format(_record(
        query="学号2023123456", event="test",
    )))
    assert "2023123456" not in json.dumps(payload, ensure_ascii=False)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_console_formatter.py -q`
Expected: FAIL，`ImportError: cannot import name 'ConsoleFormatter'`

- [ ] **Step 3: 实现 ConsoleFormatter 与 JSON 字段**

在 `backend/app/core/logging.py` 中：

1. 新增 `import os`, `import time`, `from app.core import trace`。
2. 新增 `_ANSI` 常量与 `_color_enabled()`。
3. 新增 `ConsoleFormatter`：
   - `__init__(self, use_color: bool = False)` 保存开关。
   - `format(record)` 生成 `HH:MM:SS.mmm LEVEL [trace8] indent stage status message duration fields`。
   - `stage` 优先取 `getattr(record, "stage", "")`，否则 `trace.current_stage()`；`node.` 前缀去掉。
   - `depth` 优先取 record，否则 `trace.current_depth()`。
   - `trace_id` 优先取 record，否则 `observability.current_trace_id()`；全 0 显示 `--------`。
   - 字段白名单：`query_len query_hash session_id user_id route route_source decision changed skipped queries candidates recalled kept top confidence degraded evidence chars citations reason facets model node ttft_ms tokens_in tokens_out tokens hits folded retry pages ocr_pages tables chunks batches vectors before_tokens after_tokens summary_chars case_id turn hit status method path error`。
   - `ms` 显示为 `332ms` / `2.72s`，不重复进字段列表。
   - 值截断到 `cfg("logging.console_max_value_chars", 120)`，去掉 `\r\n`。
   - 不调用 `formatException`；错误只显示 `error=类型`。
4. `JsonFormatter.format`：
   - 顶部读取 `stage = getattr(record, "stage", None) or trace.current_stage() or None`。
   - `depth = getattr(record, "depth", None)`；若 `None` 取 `trace.current_depth()`。
   - `stage_id = getattr(record, "stage_id", None) or trace.current_span_id() or None`。
   - `parent_stage_id = getattr(record, "parent_stage_id", None) or None`。
   - `trace_id` 优先取 `getattr(record, "trace_id", None)`，否则 `observability.current_trace_id()`；`span_id` 同理。
   - 把上述字段按存在与否加到 payload。
   - `reserved` 增加 `trace_id span_id stage depth stage_id parent_stage_id layer ok`，避免重复进 `extra`。
5. `install()`：
   - 读取 `console_level`、`console_format`、`console_color`。
   - 控制台 handler：`console_format == "json"` 用 `JsonFormatter`，否则 `ConsoleFormatter(use_color=_color_enabled())`；`setLevel(console_level)`。
   - 文件 handler 继续 `JsonFormatter`。
   - 噪音库列表追加 `bm25s`。
   - 可选（P3 再开）：把 `uvicorn` / `uvicorn.error` / `uvicorn.access` 的 handler 清空并 `propagate=True`。

`app.yaml` 的 `logging` 段追加：

```yaml
  console_level: INFO
  console_format: text
  console_color: auto
  console_max_value_chars: 120
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_console_formatter.py backend/tests/unit/test_logging_trace_id.py -q`
Expected: 新增用例通过；`test_logging_trace_id` 里原有的全 0 断言仍通过（此时还没有中间件接入）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/logging.py backend/app/config/app.yaml backend/tests/unit/test_console_formatter.py
git commit -m "[feat] 控制台文本日志与控制台/文件双格式器"
```

---

## Task 3: trace_id 对齐与 HTTP 中间件

**Files:**
- Modify: `backend/app/core/observability.py:128-161`
- Modify: `backend/app/main.py:156-222`
- Modify: `backend/app/api/chat.py:27-54`
- Modify: `backend/tests/unit/test_logging_trace_id.py`
- Test: `backend/tests/unit/test_http_trace.py`

**Interfaces:**
- Consumes: `trace.current_trace_id()`, `trace.current_span_id()`, `trace.begin_trace()`, `trace.trace_span()`.
- Produces: `observability.current_trace_id()` 优先返回本地 trace；`observability.current_span_id()` 优先 Langfuse、回退本地；HTTP 日志 `stage="http"`。

- [ ] **Step 1: 写失败测试**

```python
"""HTTP 中间件的 trace 与日志。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import observability, trace
from app.main import trace_middleware


def _app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/ping")
    async def ping():
        assert trace.current_trace_id() != ""
        return {"ok": True}

    return app


def test_http_trace_id_is_real_and_logged(caplog):
    observability._reset_for_test()
    app = _app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO):
            response = client.get("/ping")

    tid = response.headers["x-trace-id"]
    assert len(tid) == 32 and tid != "0" * 32
    http_done = [r for r in caplog.records
                 if getattr(r, "stage", "") == "http"
                 and getattr(r, "event", "") == "stage.done"]
    assert http_done, "没有 http 完成日志"
    assert http_done[-1].status == 200
    assert http_done[-1].trace_id == tid


def test_incoming_traceparent_is_reused():
    incoming = "a" * 32
    observability._reset_for_test()
    app = _app()
    with TestClient(app) as client:
        response = client.get(
            "/ping",
            headers={"traceparent": f"00-{incoming}-{'b' * 16}-01"},
        )
    assert response.headers["x-trace-id"] == incoming
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_http_trace.py -q`
Expected: FAIL，`x-trace-id` 为全 0 或没有 `stage=http` 日志。

- [ ] **Step 3: 实现对齐与中间件**

`observability.py`：

```python
from app.core import trace

def current_trace_id() -> str:
    local = trace.current_trace_id()
    if local:
        return local
    # 原有 Langfuse 逻辑 + 全 0 兜底保持不变
    ...

def current_span_id() -> str:
    # 原有 Langfuse 逻辑；如果取不到有效值，回退本地
    ...
    local = trace.current_span_id()
    return local or _ALL_ZERO_SPAN
```

`main.py` 的 `trace_middleware`：

```python
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    carrier = {k.lower(): v for k, v in request.headers.items()}
    incoming = _incoming_trace_id(carrier)
    tid = incoming or trace.new_trace_id()

    with trace.begin_trace(tid), trace.trace_span("http", layer="http") as stage:
        stage.set(method=request.method, path=request.url.path)
        client = observability.get_client()

        if client is None:
            try:
                response = await call_next(request)
            except BaseException as e:
                stage.set(ok=False, error=type(e).__name__)
                raise
            stage.set(status=response.status_code,
                      ok=response.status_code < 500)
            response.headers["x-trace-id"] = tid
            response.headers["traceparent"] = (
                f"00-{tid}-{trace.current_span_id()}-01")
            return response

        pending: BaseException | None = None
        response = None
        with client.start_as_current_observation(
            as_type="span",
            name=f"{request.method} {request.url.path}",
            trace_context={"trace_id": tid},
            input=None, output=None,
        ) as span:
            try:
                response = await call_next(request)
            except BaseException as e:
                span.update(level="ERROR", status_message=type(e).__name__)
                pending = e

            if response is not None:
                span.update(metadata={
                    "http.method": request.method,
                    "http.route": request.url.path,
                    "http.status_code": response.status_code,
                })
                if response.status_code >= 500:
                    span.update(level="ERROR",
                                status_message=f"HTTP {response.status_code}")
                span_id = observability.current_span_id()
                response.headers["x-trace-id"] = tid
                response.headers["traceparent"] = f"00-{tid}-{span_id}-01"
                stage.set(status=response.status_code,
                          ok=response.status_code < 500)

        if pending is not None:
            raise pending
        return response
```

注意：

- `stage.set(ok=...)` 会覆盖 `trace_span` 默认的 `ok=True`；5xx 的状态图标为失败。
- HTTP stage 是根 span，depth=0；图节点 depth=1。
- 不要记录 query string / body。

`api/chat.py` 的生成器：

```python
async def generator():
    with trace.begin_trace(trace_id), observability.attach_trace(trace_id, span_id):
        async for frame in chat_service.stream_chat(...):
            yield frame
```

- [ ] **Step 4: 更新 trace_id 契约测试**

`test_logging_trace_id.py` 增加：

```python
def test_local_trace_id_is_used_inside_trace():
    from app.core import trace
    with trace.begin_trace():
        payload = _format("hello")
    assert len(payload["trace_id"]) == 32
    assert payload["trace_id"] != "0" * 32
```

保留原有「无 trace 上下文时全 0」用例。

- [ ] **Step 5: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_http_trace.py backend/tests/unit/test_logging_trace_id.py -q`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add backend/app/core/observability.py backend/app/main.py backend/app/api/chat.py backend/tests/unit/test_http_trace.py backend/tests/unit/test_logging_trace_id.py
git commit -m "[feat] HTTP 中间件接入本地 trace 并对齐 Langfuse"
```

---

## Task 4: 图节点统一计时与日志

**Files:**
- Modify: `backend/app/graph/builder.py:83-159`
- Modify: `backend/app/graph/node_io.py`
- Test: `backend/tests/unit/test_node_log_fields.py`

**Interfaces:**
- Consumes: `trace.trace_span`, `trace.current_*`.
- Produces: `node_log_fields(node: str, result: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]`.

- [ ] **Step 1: 写失败测试**

```python
"""图节点日志字段。"""
from __future__ import annotations

import logging

from app.graph.builder import traced
from app.graph.node_io import node_log_fields


def test_node_log_fields_have_no_body():
    state = {"query": "转专业条件是什么"}
    result = {
        "resolved_query": "转专业的条件是什么",
        "candidates": [object(), object()],
        "trace": [{"node": "resolve", "ms": 12, "recalled": 0, "degraded": None}],
    }
    fields = node_log_fields("resolve", result, state)
    assert fields["query_len"] == 7
    assert fields["changed"] is True
    assert "转专业" not in repr(fields)


async def test_traced_logs_even_without_langfuse(caplog):
    async def fake(state):
        return {
            "route": "knowledge",
            "route_source": "rules",
            "trace": [{"node": "route", "ms": 3, "recalled": 0, "degraded": None}],
        }

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        result = await traced("route", fake)({"query": "q"})

    assert result["route"] == "knowledge"
    recs = [r for r in caplog.records if getattr(r, "event", "") == "stage.done"]
    assert recs, "没有节点完成日志"
    assert recs[-1].stage == "node.route"
    assert recs[-1].route == "knowledge"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_node_log_fields.py -q`
Expected: FAIL，`node_log_fields` 不存在。

- [ ] **Step 3: 实现 `node_log_fields`**

在 `node_io.py` 增加：

```python
def node_log_fields(node: str, result: Mapping[str, Any],
                    state: Mapping[str, Any]) -> dict[str, Any]:
    """只提取可打印的标量字段 —— 绝不放 query/answer 正文。"""
    fields: dict[str, Any] = {}
    entries = result.get("trace") or []
    last = entries[-1] if entries else {}
    if last.get("degraded"):
        fields["degraded"] = str(last["degraded"])

    if node == "resolve":
        original = str(state.get("query", ""))
        resolved = str(result.get("resolved_query", ""))
        fields["query_len"] = len(original)
        fields["changed"] = bool(resolved and resolved != original)
        fields["skipped"] = bool(result.get("resolve_skipped_reason"))
    elif node == "route":
        fields["route"] = result.get("route", "")
        fields["source"] = result.get("route_source", "")
        fields["clarify_skipped"] = bool(result.get("clarify_skipped"))
    elif node == "rewrite":
        fields["queries"] = len(result.get("retrieval_queries") or [])
    elif node == "retrieve":
        fields["candidates"] = len(result.get("candidates") or [])
    elif node == "rerank":
        reranked = result.get("reranked") or []
        fields["kept"] = len(reranked)
        fields["top"] = round(float(reranked[0].score), 4) if reranked else 0.0
        fields["confidence"] = result.get("retrieval_confidence")
    elif node == "build_context":
        fields["evidence"] = len(result.get("evidence") or [])
        fields["chars"] = len(result.get("context") or "")
    elif node == "generate":
        fields["decision"] = result.get("decision", "")
        fields["chars"] = len(result.get("answer") or "")
    elif node == "cite":
        fields["citations"] = len(result.get("citations") or [])
    elif node == "refuse":
        fields["reason"] = result.get("refusal_reason", "")
    elif node == "chat":
        fields["decision"] = result.get("decision", "")
        fields["chars"] = len(result.get("answer") or "")
    elif node == "clarify":
        fields["facets"] = len(result.get("clarify_facets") or [])
    return {k: v for k, v in fields.items() if v is not None}
```

- [ ] **Step 4: 改造 `traced()`**

把 `builder.py` 的 `traced` 改为无论 Langfuse 是否启用都先进入 `trace.trace_span`：

```python
@functools.wraps(fn)
async def _wrapper(state: RAGState) -> dict:
    client = observability.get_client()

    with observability.node_context(node_name):
        with trace.trace_span(f"node.{node_name}", layer="node") as stage:
            if client is None:
                result = await fn(state)
            else:
                failure: BaseException | None = None
                with client.start_as_current_observation(
                    as_type="span", name=f"node.{node_name}",
                    input=node_input(node_name, state), output=None,
                ) as obs:
                    try:
                        result = await fn(state)
                    except Exception as e:  # noqa: BLE001
                        obs.update(level="ERROR",
                                   status_message=type(e).__name__,
                                   metadata={"rag.error": type(e).__name__})
                        failure = e
                    else:
                        if isinstance(result, dict):
                            out = node_output(node_name, result)
                            entries = result.get("trace") or []
                            if entries:
                                last = entries[-1]
                                meta = {
                                    "rag.duration_ms": int(last.get("ms", 0)),
                                    "rag.recalled": int(last.get("recalled", 0)),
                                }
                                degraded = last.get("degraded")
                                if degraded:
                                    meta["rag.degraded"] = str(degraded)
                                obs.update(metadata=meta, output=out)
                            else:
                                obs.update(output=out)
                if failure is not None:
                    raise failure

            if isinstance(result, dict):
                stage.set(**node_log_fields(node_name, result, state))
            return result
```

- [ ] **Step 5: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_node_log_fields.py backend/tests/integration/test_stage_events.py -q`
Expected: 全部通过（stage 事件不受影响）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/graph/builder.py backend/app/graph/node_io.py backend/tests/unit/test_node_log_fields.py
git commit -m "[feat] 图节点统一输出阶段耗时与关键字段"
```

---

## Task 5: LLM 阶段、TTFT 与 tokens

**Files:**
- Modify: `backend/app/core/llm.py:78-253`
- Modify: `backend/app/services/context_service.py:221-235`
- Test: `backend/tests/unit/test_llm_trace.py`

**Interfaces:**
- Consumes: `trace.trace_span`.
- Produces: LLM 日志 `stage="llm"`，字段 `node/model/messages/max_tokens/ttft_ms/tokens_in/tokens_out/tokens/chars`。

- [ ] **Step 1: 写失败测试**

```python
"""LLM 调用的阶段日志。"""
from __future__ import annotations

import logging

from app.core import llm


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"content": "你好"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _Resp()


async def test_complete_logs_tokens_without_prompt(monkeypatch, caplog):
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm, "_base", lambda: "http://example.test")
    monkeypatch.setattr(llm, "_headers", lambda: {})

    with caplog.at_level(logging.INFO, logger="app.core.trace"):
        text = await llm.complete(
            [{"role": "user", "content": "SECRET_PROMPT_9f3a"}],
            timeout=1,
        )

    assert text == "你好"
    recs = [r for r in caplog.records if getattr(r, "stage", "") == "llm"
            and getattr(r, "event", "") == "stage.done"]
    assert recs, "没有 llm 完成日志"
    rec = recs[-1]
    assert rec.tokens_in == 10
    assert rec.tokens_out == 2
    assert rec.chars == 2
    assert "SECRET_PROMPT_9f3a" not in caplog.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_llm_trace.py -q`
Expected: FAIL，没有 `stage="llm"` 日志。

- [ ] **Step 3: 实现 `complete` 的 span**

在 `complete()` 外层包：

```python
with trace.trace_span(
    "llm", layer="llm", node=_current_node(), model=_model(),
    messages=len(messages), max_tokens=body.get("max_tokens"),
) as stage:
    # 原有 Langfuse generation 与 httpx 调用
    ...
    stage.set(tokens_in=usage.get("input", 0),
              tokens_out=usage.get("output", 0),
              tokens=f"{usage.get('input', 0)}/{usage.get('output', 0)}",
              chars=len(content))
    return content
```

`usage` 在 try 内部赋值；把它初始化到外层，避免 `UnboundLocalError`。

- [ ] **Step 4: 实现 `stream_raw` 的 span 与 TTFT**

```python
with trace.trace_span(
    "llm", layer="llm", node=_current_node(), model=_model(),
    messages=len(messages), max_tokens=body.get("max_tokens"), stream=True,
) as stage:
    first_token_at: float | None = None
    usage: dict[str, int] = {}
    collected: list[str] = []
    try:
        ...  # 原有逻辑；usage 更新处保留
        if piece:
            if first_token_at is None:
                first_token_at = time.perf_counter()
                stage.set(ttft_ms=int((first_token_at - stage.started) * 1000))
            collected.append(piece)
            yield piece
    finally:
        stage.set(tokens_in=usage.get("input", 0),
                  tokens_out=usage.get("output", 0),
                  tokens=f"{usage.get('input', 0)}/{usage.get('output', 0)}",
                  chars=sum(len(p) for p in collected))
        if obs is not None:
            obs.end()
```

注意：`usage` 在 chunk 处理里更新；`stage.set` 在 `finally` 里设置最终字段。`obs.end()` 保持在同一个 `finally`。

- [ ] **Step 5: 压缩 LLM 的 node 上下文**

`context_service._summarize` 包 `observability.node_context("compaction")`：

```python
from app.core import observability

async def _summarize(previous: str, merged: list[Message]) -> str:
    prompt = render(...)
    with observability.node_context("compaction"):
        text = await llm.complete(...)
    return text
```

- [ ] **Step 6: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_llm_trace.py backend/tests/unit/test_langfuse_generation.py -q`
Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add backend/app/core/llm.py backend/app/services/context_service.py backend/tests/unit/test_llm_trace.py
git commit -m "[feat] LLM 阶段输出 TTFT 与 token 统计"
```

---

## Task 6: 检索子阶段

**Files:**
- Modify: `backend/app/retrieval/embedding.py:100-102`
- Modify: `backend/app/retrieval/search.py:197-292`
- Modify: `backend/app/retrieval/fusion.py:21-58`
- Modify: `backend/app/retrieval/reranker.py:257-280`
- Test: `backend/tests/unit/test_retrieval_trace.py`

**Interfaces:**
- Produces: `stage="retrieve.embed_query" / "retrieve.vector" / "retrieve.bm25" / "retrieve.rrf" / "rerank"`，默认 DEBUG。

- [ ] **Step 1: 写失败测试**

```python
"""检索子阶段日志。"""
from __future__ import annotations

import logging

from app.core import trace
from app.graph.state import Chunk
from app.retrieval import fusion, reranker


def _chunk(cid: str, score: float = 0.0) -> Chunk:
    return Chunk(chunk_id=cid, text="t", score=score)


def test_rrf_logs_counts(caplog):
    with trace.begin_trace():
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            out = fusion.weighted_rrf([
                ({"text": "a", "target": "both", "weight": 1.0, "source": "x"},
                 [_chunk("c1"), _chunk("c2")]),
            ])

    assert [c.chunk_id for c in out] == ["c1", "c2"]
    rec = [r for r in caplog.records if getattr(r, "stage", "") == "retrieve.rrf"][-1]
    assert rec.inputs == 2
    assert rec.outputs == 2


async def test_rerank_logs_candidates_and_kept(monkeypatch, caplog):
    def fake_sync(query, chunks, *, top_k=None):
        return reranker.RerankResult(
            chunks=chunks[:1], confidence=0.9, degraded=False)

    monkeypatch.setattr(reranker, "rerank_sync", fake_sync)

    with trace.begin_trace():
        with caplog.at_level(logging.DEBUG, logger="app.core.trace"):
            result = await reranker.rerank("q", [_chunk("c1"), _chunk("c2")])

    assert len(result.chunks) == 1
    rec = [r for r in caplog.records if getattr(r, "stage", "") == "rerank"][-1]
    assert rec.candidates == 2
    assert rec.kept == 1
    assert rec.confidence == 0.9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_retrieval_trace.py -q`
Expected: FAIL，没有对应 stage 日志。

- [ ] **Step 3: 实现各子阶段**

`fusion.weighted_rrf`：

```python
with trace.trace_span("retrieve.rrf", level=logging.DEBUG,
                      rrf_k=k, inputs=sum(len(h) for _, h in ranked_lists)) as stage:
    ...  # 原有逻辑
    stage.set(outputs=len(out))
return out
```

`reranker.rerank`：

```python
async def rerank(...):
    async with _get_semaphore():
        with trace.trace_span("rerank", level=logging.DEBUG,
                              candidates=len(chunks)) as stage:
            result = await asyncio.to_thread(rerank_sync, query, chunks, top_k=top_k)
            stage.set(kept=len(result.chunks),
                      top=round(float(result.chunks[0].score), 4) if result.chunks else 0.0,
                      confidence=result.confidence,
                      degraded=result.degraded_kind or ("degraded" if result.degraded else None))
            return result
```

`embedding.embed_query`：

```python
async def embed_query(text: str) -> list[float]:
    with trace.trace_span("retrieve.embed_query", level=logging.DEBUG) as stage:
        vectors = await embed_texts([text])
        stage.set(dim=len(vectors[0]) if vectors else 0)
        return vectors[0]
```

`search.vector_retrieve` / `bm25_retrieve`：在函数体外包 `trace_span("retrieve.vector"/"retrieve.bm25", level=DEBUG)`，结束时 `stage.set(k=k, hits=len(result), retry=..., folded=...)`。`retry` 用局部变量记录是否走了 `_attempt(oversample_retry)`；`folded` 取最终 `len(chunks)`。

- [ ] **Step 4: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit/test_retrieval_trace.py -q`
Expected: 2 passed。

- [ ] **Step 5: 提交**

```bash
git add backend/app/retrieval/embedding.py backend/app/retrieval/search.py backend/app/retrieval/fusion.py backend/app/retrieval/reranker.py backend/tests/unit/test_retrieval_trace.py
git commit -m "[feat] 检索子阶段输出耗时与召回数量"
```

---

## Task 7: chat 汇总与上下文压缩

**Files:**
- Modify: `backend/app/services/chat_service.py:161-338`
- Modify: `backend/app/services/context_service.py:129-218`
- Test: `backend/tests/integration/test_chat_summary_log.py`

**Interfaces:**
- Produces: `chat.first_token` / `chat.done` / `chat.error` / `context.compact` 日志。

- [ ] **Step 1: 写失败测试**

复用 `test_stage_events.py` 的 `ScriptedGraph` / `session` fixture。新建测试：

```python
"""chat 首字与完成汇总日志。"""
from __future__ import annotations

import logging

import pytest

from app.services import chat_service
from tests.integration.test_stage_events import (  # noqa: F401
    _frames, _never_disconnected, script_for, session, task_start, task_result,
)


async def test_chat_done_logs_summary(session, _install, caplog):
    final = {"route": "knowledge", "refused": False, "decision": "ANSWERED",
             "answer": "答案", "citations": [], "reranked": []}
    _install(script_for(["resolve", "route", "rewrite", "retrieve", "rerank",
                         "build_context", "generate", "cite"], final))
    with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
        await _frames(session)

    done = [r for r in caplog.records if getattr(r, "event", "") == "chat.done"]
    assert done, "没有 chat.done 日志"
    rec = done[-1]
    assert rec.route == "knowledge"
    assert rec.decision == "ANSWERED"
    assert rec.latency_ms >= 0
```

注意：`_install` 是 `test_stage_events.py` 里的 fixture，需要在新文件里定义同名 fixture 或直接复用 monkeypatch。实现时按现有 fixture 方式处理。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_chat_summary_log.py -q`
Expected: FAIL，没有 `chat.done`。

- [ ] **Step 3: 实现 chat 日志**

`stream_chat` 开头增加：

```python
ttft_ms: int | None = None
```

第一次发 token 的两处（`custom` 分支和兜底补发）都调用：

```python
if ttft_ms is None:
    ttft_ms = int((time.perf_counter() - started) * 1000)
    logger.info("首字", extra={"event": "chat.first_token", "stage": "chat",
                               "layer": "chat", "ttft_ms": ttft_ms,
                               "route": final.get("route", "")})
```

正常收尾、`yield sse("done", ...)` 之前：

```python
logger.info("完成", extra={
    "event": "chat.done", "stage": "chat", "layer": "chat",
    "route": final.get("route", ""),
    "decision": decision,
    "refused": bool(final.get("refused")),
    "latency_ms": latency_ms,
    "citations": len(final.get("citations") or []),
    "ttft_ms": ttft_ms,
})
```

断连分支增加 `stage="chat", layer="chat", elapsed_ms=...`；`chat.failed` 增加 `stage="chat", layer="chat"`。

- [ ] **Step 4: 实现上下文压缩 span**

`context_service._compact` 在 `merged > 0` 后：

```python
before_tokens = history_tokens(summary, messages)
with trace.trace_span("context.compact", layer="context") as stage:
    new_summary = await _summarize(summary, messages[:merged])
    stage.set(before_tokens=before_tokens,
              after_tokens=history_tokens(new_summary, messages[merged:]),
              messages=merged,
              summary_chars=len(new_summary))
```

把 `load_context` 里现有的 `logger.info("滚动压缩完成", ...)` 删掉，避免与 span 的完成行重复；失败 warning 保留。

- [ ] **Step 5: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_chat_summary_log.py backend/tests/integration/test_compaction.py -q`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/chat_service.py backend/app/services/context_service.py backend/tests/integration/test_chat_summary_log.py
git commit -m "[feat] chat 首字/完成汇总与压缩阶段日志"
```

---

## Task 8: 文档摄入与索引

**Files:**
- Modify: `backend/app/ingestion/pipeline.py:125-284`
- Modify: `backend/app/services/index_service.py:36-52`
- Modify: `backend/app/services/document_service.py:140-157`
- Test: `backend/tests/integration/test_ingest_trace.py`（新增，若现有摄入测试可复用则扩展）

**Interfaces:**
- Produces: `ingest.validate/dedupe/store/parse/clean/chunk/enrich/embed/index_write/activate/done` 日志。

- [ ] **Step 1: 写失败测试**

用现有吸入测试的最小夹具（见 `backend/tests/integration/` 中已有的文档上传用例）构造一个小 txt 文件，调用 `pipeline.ingest`，断言 caplog 中出现：

```python
stages = {getattr(r, "stage", "") for r in caplog.records
          if getattr(r, "event", "") == "stage.done"}
assert {"ingest.parse", "ingest.chunk", "ingest.embed", "ingest.index_write"} <= stages
```

如果现有测试依赖真实 PG/Chroma，按现有 fixture 复用；不新造 mock 数据库。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_ingest_trace.py -q`
Expected: FAIL，阶段日志缺失。

- [ ] **Step 3: 实现摄入阶段**

`pipeline.py`：

- 把现有 `async def ingest` 重命名为 `_ingest`，新增：

```python
async def ingest(req: IngestRequest) -> IngestOutcome:
    with trace.begin_trace(req.trace_id):
        return await _ingest(req)
```

- 在 `_ingest` 中按阶段包 `trace.trace_span`：
  - `ingest.validate`：`is_supported`；`stage.set(file_type=extension_of(req.filename))`。
  - `ingest.dedupe`：`md5_of` + `find_existing_by_md5`；`stage.set(md5=digest, duplicate=...)`。
  - `ingest.store`：复制 + 插行；`stage.set(doc_id=doc_id, version=version, size=...)`。
  - `ingest.parse`：`load_document`；`stage.set(pages=len(result.pages), ocr_pages=len(result.ocr_pages), missing_pages=len(result.missing_pages), source=result.source)`。
  - `ingest.clean`：`build_normalized_text`；`stage.set(chars_in=..., chars_out=len(normalized), pages=len(page_texts))`。
  - `ingest.chunk`：`chunk_text`；`stage.set(chunks=len(spans), chars=len(normalized))`。
  - `ingest.enrich`：构建 `entries` 的循环；`stage.set(chunks=len(entries), chapters=len(marks))`。
  - `ingest.activate`：最后的 DB 更新；`stage.set(doc_id=doc_id, chunks=written_chunks)`。
- 现有 `ingest.done` / `ingest.failed` 日志补 `stage="ingest"`, `layer="ingest"`，并加 `doc_id` / `chunks` / `pages` / `ms`。
- 失败路径不要拆掉现有 `logger.exception` 与补偿删除。

`index_service.build_index`：

```python
with trace.trace_span("ingest.embed", layer="ingest") as stage:
    vectors = await embed_texts(texts)
    stage.set(chunks=len(texts), dim=len(vectors[0]) if vectors else 0)

with trace.trace_span("ingest.index_write", layer="ingest") as stage:
    vector.upsert(...)
    bm25.add(...)
    stage.set(vectors=len(vectors), bm25=len(entries))
```

`document_service.schedule_ingest`：

```python
async def _run() -> None:
    with trace.begin_trace(trace_id), observability.attach_trace(trace_id, span_id):
        logger.info("开始", extra={"event": "ingest.schedule", "stage": "ingest.schedule",
                                   "layer": "ingest", "task_id": req.task_id,
                                   "filename": req.filename})
        try:
            await ingest(req)
        finally:
            req.source_path.unlink(missing_ok=True)
```

- [ ] **Step 4: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_ingest_trace.py -q`
Expected: PASS（若环境缺 PG，记录为环境阻塞，继续用 Task 11 的 E2E）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/ingestion/pipeline.py backend/app/services/index_service.py backend/app/services/document_service.py backend/tests/integration/test_ingest_trace.py
git commit -m "[feat] 文档摄入阶段日志与后台 trace 续接"
```

---

## Task 9: 评测链路

**Files:**
- Modify: `backend/app/eval/runner.py:183-338`
- Test: `backend/tests/integration/test_eval_trace.py`（新增或扩展现有评测测试）

**Interfaces:**
- Produces: `stage="eval.run"`（INFO）、`stage="eval.case"`（DEBUG）。

- [ ] **Step 1: 写失败测试**

复用现有评测集成测试夹具，跑一轮最小评测，断言：

```python
assert any(getattr(r, "stage", "") == "eval.run" for r in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_eval_trace.py -q`
Expected: FAIL，无 `eval.run`。

- [ ] **Step 3: 实现**

`run_eval` 重命名为 `_run_eval`，新增：

```python
async def run_eval(run_id: str) -> None:
    with trace.begin_trace():
        await _run_eval(run_id)
```

在 `_run_eval` 的 `for case in rows` 循环外包：

```python
with trace.trace_span("eval.case", layer="eval", level=logging.DEBUG,
                      case_id=case["id"]) as stage:
    ...  # 原有单题逻辑
    stage.set(route=final.get("route", ""), hit=...)
```

若缩进过大，可在循环内用 `with trace.trace_span(...)` 只包住 `_drive_to_end` 与指标计算的关键段，但必须保证 `stage` 完成日志能拿到 `route`。

`eval.done` / `eval.failed` 日志补 `stage="eval.run"`, `layer="eval"`。

- [ ] **Step 4: 跑测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration/test_eval_trace.py backend/tests/integration/test_eval_api.py -q`
Expected: 全部通过（或按环境记录阻塞）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/eval/runner.py backend/tests/integration/test_eval_trace.py
git commit -m "[feat] 评测链路接入 trace 与阶段日志"
```

---

## Task 10: uvicorn 日志统一与使用说明

**Files:**
- Modify: `backend/app/core/logging.py:108-141`
- Create: `docs/日志追踪使用说明.md`

- [ ] **Step 1: 统一 uvicorn 日志**

在 `install()` 末尾：

```python
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = True
    lg.setLevel(logging.INFO)
```

如果实测出现重复或丢日志，改为在启动命令加 `--no-access-log`，并保留中间件的 HTTP 完成行。

- [ ] **Step 2: 写使用说明**

`docs/日志追踪使用说明.md` 至少包含：

- 控制台各列含义。
- 默认 INFO 能看到什么；`LOG_LEVEL=DEBUG` 能看到什么。
- `trace_id` 从哪里拿、如何与响应头/Langfuse 对应。
- 示例输出（问答、摄入、评测各一段）。
- 如何关闭颜色：`console_color: never`。
- 如何只看文件日志：`console_level: WARNING`。
- 不要记录正文的约束。

- [ ] **Step 3: 提交**

```bash
git add backend/app/core/logging.py docs/日志追踪使用说明.md
git commit -m "[docs] 统一 uvicorn 日志并补充日志追踪使用说明"
```

---

## Task 11: 全流程验证

**Files:** 无代码改动（只跑验证，必要时修 bug）。

- [ ] **Step 1: 跑单元测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/unit -q`
Expected: 全部通过。

- [ ] **Step 2: 跑集成测试**

Run: `cd D:/Knowledge_rag_system && .venv/Scripts/python.exe -m pytest backend/tests/integration -q`
Expected: 全部通过；若 PG/模型环境缺失，记录具体失败原因并区分「环境阻塞」与「代码失败」。

- [ ] **Step 3: HTTP + 图节点 E2E（Langfuse 关闭）**

用 TestClient + 依赖覆盖跑一次真实 `chat_service.stream_chat`（或直接发一次 HTTP 请求），捕获控制台/caplog，断言：

- 出现 `http`、`resolve`、`route`、`retrieve`、`rerank`、`generate`、`cite`、`chat` 的完成行。
- `x-trace-id` 非 0，且与日志一致。
- 不出现 query/answer 正文。

- [ ] **Step 4: HTTP + 图节点 E2E（Langfuse 开启）**

用 `tests/lf_capture.py` 的 `CapturingLangfuse` 开启 Langfuse，重复 Step 3，断言响应头 trace_id == 日志 trace_id == Langfuse trace_id。

- [ ] **Step 5: 摄入 E2E**

上传一个小 PDF/TXT，捕获控制台，断言出现 `ingest.parse`、`ingest.chunk`、`ingest.embed`、`ingest.index_write`、`ingest.activate`，且 `ingest.done` 有 chunks。

- [ ] **Step 6: 评测 E2E**

跑一轮最小评测，断言出现 `eval.run` 汇总；`LOG_LEVEL=DEBUG` 时出现 `eval.case`。

- [ ] **Step 7: 日志文件校验**

解析 `logs/app.jsonl` 最后 N 行，断言每行合法 JSON，包含 `trace_id`、`event`、`stage`；grep 哨兵字符串确认正文未泄露。

- [ ] **Step 8: 最终提交与推送**

```bash
git add backend/app backend/tests backend/app/config/app.yaml docs/日志追踪使用说明.md docs/superpowers/plans/2026-10-19-log-tracing.md
git commit -m "[test] 全链路日志追踪验证与收尾"
git push origin refactor/campus-rag
```

若 Step 1-7 有代码修复，先把修复单独提交，再执行最终提交。
