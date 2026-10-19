"""HTTP 中间件的 trace 与日志。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
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

    @app.get("/fail")
    async def fail():
        return JSONResponse(status_code=500, content={})

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"id": item_id}

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


def test_5xx_is_logged_as_error(caplog):
    observability._reset_for_test()
    app = _app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO):
            response = client.get("/fail")

    assert response.status_code == 500
    http_done = [r for r in caplog.records
                 if getattr(r, "stage", "") == "http"
                 and getattr(r, "event", "") == "stage.done"]
    assert http_done, "没有 http 完成日志"
    assert http_done[-1].levelno == logging.ERROR
    assert http_done[-1].status == 500


def test_langfuse_enabled_keeps_header_and_log_id(monkeypatch, caplog):
    from tests.lf_capture import CapturingLangfuse

    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")

        app = _app()
        with TestClient(app) as client:
            with caplog.at_level(logging.INFO):
                response = client.get("/ping")

        observability.shutdown()
        observability._reset_for_test()

    tid = response.headers["x-trace-id"]
    assert len(tid) == 32 and tid != "0" * 32
    assert cap.bodies(), "Langfuse 没有收到任何数据"
    http_done = [r for r in caplog.records
                 if getattr(r, "stage", "") == "http"
                 and getattr(r, "event", "") == "stage.done"]
    assert http_done, "没有 http 完成日志"
    assert http_done[-1].trace_id == tid
    # header 的 span 必须与日志里的 span 一致，便于从响应头跳回日志行。
    assert response.headers["traceparent"].split("-")[2] == http_done[-1].span_id


def test_http_path_uses_route_template(caplog):
    """带 id 的路径必须记路由模板，不能把具体 id 变成高基数日志字段。"""
    observability._reset_for_test()
    app = _app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO):
            response = client.get("/items/abc123")

    assert response.status_code == 200
    http_done = [r for r in caplog.records
                 if getattr(r, "stage", "") == "http"
                 and getattr(r, "event", "") == "stage.done"]
    assert http_done[-1].path == "/items/{item_id}"


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
