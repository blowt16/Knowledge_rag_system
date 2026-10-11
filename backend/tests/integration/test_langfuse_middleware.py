"""HTTP 中间件的 trace_id 与 5xx 标记（【方案 §5.3 / §8.3】）。

两种失败模式：
  - 中间件里取不到 trace_id（Starlette 与 SDK 是否同一 OTel context，文档没写）
  - 5xx 没标 level=ERROR → 错误率面板读不到
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import observability
from tests.lf_capture import CapturingLangfuse


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_trace_id_consistent_across_middleware_and_header(capture):
    """★ 中间件内 / 端点内 / 响应头三处必须是同一个 id（实测通过，这里钉住）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)
    seen = {}

    @app.get("/ping")
    async def ping():
        seen["in_endpoint"] = observability.current_trace_id()
        return {"ok": True}

    with TestClient(app) as tc:
        r = tc.get("/ping")

    header_id = r.headers.get("x-trace-id")
    assert header_id and header_id != "0" * 32, "响应头没带 trace_id"
    assert seen["in_endpoint"] == header_id, (
        f"端点内({seen['in_endpoint']}) 与响应头({header_id}) 不一致"
    )


def test_5xx_response_is_marked_error(capture):
    """5xx 必须把 span 标成 `level=ERROR` —— 错误率面板靠它（【方案 §8.3】）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/boom")
    async def boom():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"error": "x"})

    with TestClient(app) as tc:
        tc.get("/boom")

    observability.get_client().flush()
    body = capture.body()
    assert "ERROR" in body, "5xx 没被标成 ERROR —— 错误率面板会恒为 0"


def test_2xx_is_not_marked_error(capture):
    """正控：正常请求不能标 ERROR（否则错误率恒为 100%）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(app) as tc:
        tc.get("/ok")

    observability.get_client().flush()
    assert "ERROR" not in capture.body()


def test_unhandled_exception_does_not_leak_message_to_cloud(capture):
    """★ 端点抛异常时，异常 message **不得**进入发出去的字节。

    ⚠️ 实测（Task 5）：Langfuse 内部用 OTel 的 `start_as_current_span`，
       其 `use_span` 默认 `record_exception=True` 且**无参数可关** ——
       异常一旦穿过 with 块，`exception.message`（= `str(e)`）与整个栈都会上传。
       中间件必须在 with 内部吞掉、外部重抛。
    """
    from app.main import trace_middleware

    SECRET_IN_ERROR = "学号2023123456的回显正文"

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError(SECRET_IN_ERROR)

    with TestClient(app, raise_server_exceptions=False) as tc:
        tc.get("/boom")

    observability.get_client().flush()
    body = capture.body()
    assert body, "没发出任何东西 —— 断言会假通过"
    assert SECRET_IN_ERROR not in body, (
        f"异常 message 被发到了云端：\n{body[:500]}"
    )
