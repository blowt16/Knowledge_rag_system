"""HTTP 中间件的 trace_id 与 5xx 标记（【方案 §5.3 / §8.3】）。

两种失败模式：
  - 中间件里取不到 trace_id（Starlette 与 SDK 是否同一 OTel context，文档没写）
  - 5xx 没标 level=ERROR → 错误率面板读不到
"""
from __future__ import annotations

import re

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

    # §3.2.3.1「返回前端」：traceparent 也要回传（W3C 格式）。
    # ⚠️ 别断言两个头**字符串相等** —— 格式不同，只钉「trace 段一致」。
    tp = r.headers.get("traceparent")
    assert tp, "响应头缺 traceparent（§3.2.3.1 要求回传）"
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", tp), \
        f"traceparent 不是合法 W3C 格式：{tp}"
    assert tp.split("-")[1] == header_id, (
        f"traceparent 的 trace 段({tp.split('-')[1]}) 与 x-trace-id({header_id}) 不一致"
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


@pytest.mark.parametrize("bad", [
    "garbage",                                            # 缺段 → 原稿在这里 IndexError
    "00-abc-def-01",                                      # 段长不对
    "00-" + "a" * 32 + "-" + "b" * 16,                    # 只有三段
    "00-" + "a" * 32 + "-" + "b" * 16 + "-01-x",          # 版本 00 不允许额外字段
    "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",            # 版本 ff 保留
    "01-" + "a" * 32 + "-" + "b" * 16 + "-01",            # 未定义的版本
    "00-" + "z" * 32 + "-" + "b" * 16 + "-01",            # 非十六进制
    "00-" + "0" * 32 + "-" + "b" * 16 + "-01",            # trace-id 全 0：规范判无效
    "00-" + "a" * 32 + "-" + "0" * 16 + "-01",            # span-id 全 0：规范判无效
])
def test_malformed_traceparent_is_ignored_not_500(capture, bad):
    """畸形 `traceparent` 一律当作「没有传入上下文」，绝不 500。

    ⚠️ 这个 header **完全由客户端控制**。实测踩过：原稿
       `carrier.get("traceparent","").split("-")[1]`，`traceparent: garbage`
       直接 IndexError → 500 —— 一行请求头就能把正常请求打成 500。
       改造前用的是 OTel 官方 propagator（畸形值静默忽略），等价行为是底线。
    """
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as tc:
        r = tc.get("/ok", headers={"traceparent": bad})

    assert r.status_code < 500, f"畸形 traceparent({bad[:20]}…) 打出了 {r.status_code}"
    assert r.json() == {"ok": True}, f"畸形 traceparent({bad[:20]}…) 影响了响应内容"
    # 回退到「新建」：仍要给一个合法 id，否则前端/日志拿到空值
    fresh = r.headers.get("x-trace-id")
    assert fresh and fresh != "0" * 32, "畸形值下没回退到新建 trace"


def test_valid_traceparent_is_reused(capture):
    """★ §3.2.3.1 契约：请求头有 traceparent 则**沿用**，不是新建。

    ⚠️ 断言用哨兵值（规范里的示例 id），不是「两个头相等」—— 哨兵不可能
       来自另一条取 id 的路径（Task 4 的教训：SDK 自己也建 OTel，
       两条路径可能返回同值，同源比较证明不了什么）。
    """
    from app.main import trace_middleware

    sentinel_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    sentinel_parent = "00f067aa0ba902b7"

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(app) as tc:
        r = tc.get("/ok", headers={
            "traceparent": f"00-{sentinel_trace}-{sentinel_parent}-01"})

    assert r.status_code == 200
    assert r.headers.get("x-trace-id") == sentinel_trace, "没沿用传入的 trace_id"
    tp = r.headers.get("traceparent")
    assert tp and tp.split("-")[1] == sentinel_trace, \
        f"响应头 traceparent 的 trace 段没沿用传入值：{tp}"
