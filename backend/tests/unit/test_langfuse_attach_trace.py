"""后台任务续接（【方案 §5.4】）—— 取代 `telemetry.reattach()`。

⚠️ 「后台任务不能靠 contextvars 隐式继承」这条教训不变（§3.2.3.1）；
   换的是实现手段（Langfuse 的 `trace_context`），不是要不要显式传递。
"""
from __future__ import annotations

import pytest

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


def test_attach_trace_joins_existing_trace(capture):
    """★ 挂在别的任务里的观测，trace_id 必须与原 trace 相同。

    ⚠️⚠️ **断言必须发生在「已经脱离原上下文」之后**（本任务实测改动）。
       把断言写在 root span 的 `with` **里面**是恒绿的：
       ambient context 还在，`attach_trace` 做什么/不做什么都返回 root_trace。
       实测两种削法（去掉 `trace_context` / 削成裸 `yield`）在那个版本下**都过**。
       所以这里先退出 root 上下文、确认拿到全 0 哨兵，再进 `attach_trace`。
    """
    client = observability.get_client()

    with client.start_as_current_observation(as_type="span", name="root-req"):
        root_trace = observability.current_trace_id()
        root_span = observability.current_span_id()
        assert root_trace != "0" * 32

    # 哨兵：真的脱离干净了才有全 0；否则下面的断言证明不了任何事
    assert observability.current_trace_id() == "0" * 32, (
        "没脱离原上下文 —— 下面的断言将无法区分 `attach_trace` 有没有干活"
    )

    # 模拟后台任务：脱离当前上下文后，用 attach_trace 挂回去
    with observability.attach_trace(root_trace, root_span):
        inner = observability.current_trace_id()

    assert inner == root_trace, (
        f"后台观测没挂回原 trace：{inner} != {root_trace}"
    )


def test_attach_trace_is_noop_when_disabled(monkeypatch):
    """未启用时必须是安全的空操作，不能抛。"""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    observability.setup("campus-rag")

    with observability.attach_trace("a" * 32, "b" * 16):
        pass          # 不抛即通过
