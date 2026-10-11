"""LLM generation 观测（【方案 §5.2】）。

**两条实测踩出来的**：
  - `start_observation()` 忘 `.end()` → 观测**静默消失**，且不报错。
    流式调用（开始与结束不连续）最容易漏，所以专门测它。
  - 本项目 LLM 客户端是**手写 httpx**，Langfuse 的自动集成抓不到 usage，
    必须把 `llm.py` 里本来就在解析的 usage 手工写进观测。
"""
from __future__ import annotations

import pytest

from app.core import llm, observability
from tests.lf_capture import CapturingLangfuse


class _FakeStreamResponse:
    """假 httpx 流：喂一段 SSE。"""

    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        # ⚠️ 必须 `async for`：`_boom()` 是**异步**生成器，用同步 `for` 会
        #    立刻 `TypeError: 'async_generator' object is not iterable` ——
        #    那样用例失败在假流上，跟被测的观测生命周期毫无关系（实测）。
        async for line in self._lines:
            yield line

    async def aread(self):
        return b""


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


def test_non_stream_records_generation_with_usage(capture, monkeypatch):
    """非流式：观测里要有 model 与 token 数。"""
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "答案"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio
    out = asyncio.run(llm.complete([{"role": "user", "content": "问"}]))
    assert out == "答案"
    observability.get_client().flush()

    body = capture.body()
    assert "deepseek-flash" in body, "model 没进观测"
    # ⚠️ 断言 usage 结构而不只是数字 —— `"11" in body` 会命中 trace id / 时间戳
    assert "usage_details" in body or "usageDetails" in body, "usage 结构没进观测"
    assert "input" in body and "output" in body, "输入/输出没分开记"


def test_stream_generation_is_ended_even_on_error(capture, monkeypatch):
    """★ 核心断言：流式调用**中途抛异常**也必须 `.end()`，观测不能消失。"""
    import httpx

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, *a, **k):
            class _Ctx:
                async def __aenter__(self_inner):
                    async def _boom():
                        yield 'data: {"choices":[{"delta":{"content":"半"}}]}'
                        raise RuntimeError("上游断了")
                    return _FakeStreamResponse(_boom())
                async def __aexit__(self_inner, *a): return False
            return _Ctx()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio

    async def _drain():
        async for _ in llm.stream_raw([{"role": "user", "content": "问"}]):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_drain())

    observability.get_client().flush()
    body = capture.body()
    assert "deepseek-flash" in body, (
        "流式观测没出现 —— 多半是异常路径漏了 .end()（实测：忘 end 会静默丢，不报错）"
    )


def test_generation_records_node_name(capture, monkeypatch):
    """`rag.node` 标签要从 contextvar 取（不能再读 OTel span 名）。"""
    import httpx

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "x"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio
    from app.graph.builder import traced

    async def _node(state):
        return {"answer": await llm.complete([{"role": "user", "content": "q"}])}

    asyncio.run(traced("generate", _node)({}))
    observability.get_client().flush()

    # ⚠️ 断言 `rag.node` 标签键，而不是裸的 "generate" ——
    #    节点 span 本身就叫 `node.generate`，裸断言不加这段代码也照样过。
    body = capture.body()
    assert "rag.node" in body, "rag.node 标签没进 generation"
    assert "node.generate" in body, "节点 span 缺失"


#: 只放在**异常 message** 里。⚠️ 必须纯 ASCII —— 中文会被序列化成 `\uXXXX`，
#: `"中文" in body` 查不到，会造成**假绿**（这条坑预检时实测确认过）。
LEAK_CANARY = "EXCCANARY98765"


def test_generation_exception_records_type_only(capture, monkeypatch):
    """★ 异常 message 不许进观测 —— 预检那次实测的**回归锁**。

    ⚠️ 为什么本任务也要一条：Langfuse 内部用 OTel，`start_as_current_span` 的
       `use_span` 默认 `record_exception=True` 且 SDK 不暴露关闭参数 ——
       异常一旦从 `with` 逃出，`str(e)`（可能回显 prompt）与整条栈都会上传（Task 5 实测）。
       本任务用的是**手工观测**（`start_observation`，官方说它不进 OTel 上下文），
       预检实测（2026-10-11，本地捕获服务器，三种形状：非流式 / 嵌在节点里 / 流式）
       确认**不中招**；这条用例把那次实测变成每次都跑的凭据。

    ⚠️ 正文（prompt）是**故意照传**的（脱敏钩子在导出前打码），
       所以这里只钉异常 message，不钉 prompt。
    """
    import httpx

    class _Resp:
        status_code = 502
        text = f"upstream error echoing: {LEAK_CANARY}"

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.complete([{"role": "user", "content": "q"}]))
    observability.get_client().flush()

    body = capture.body()
    assert body, "没发出任何东西 —— 断言会假通过"
    assert "LLMError" in body, "异常类型没记"
    assert LEAK_CANARY not in body, "异常 message 带上了上游正文"


def test_node_name_resolves_without_a_client():
    """★ 没有 Langfuse 客户端时，节点名也**不能**退化成 `unknown`。

    ⚠️ 这是 Task 5 报告点出的既有小问题：旧 `llm._current_node()` 靠「读 OTel
       当前 span 名」反推，而**未启用客户端时根本不建 span**，标签就变成 `unknown`
       （过渡态：配了 OTLP 地址但没配 Langfuse 密钥时，token 指标的标签全错）。
       改读 `observability` 的 contextvar 后消失 —— 这条钉住它不回来。
       （brief 正文要求"顺手钉住"，但它的用例清单里没有，故补在此处。）

    conftest 的 autouse 夹具已删掉 `LANGFUSE_*` ⇒ 本用例天然处于"未启用"状态。
    """
    import asyncio

    from app.graph.builder import traced

    assert observability.get_client() is None, "本用例要求未启用状态"

    seen = {}

    async def _node(state):
        seen["llm"] = llm._current_node()
        seen["observability"] = observability.current_node()
        return {}

    asyncio.run(traced("rewrite", _node)({}))
    assert seen["llm"] == "rewrite", "无客户端时 llm 侧节点名退化了"
    assert seen["observability"] == "rewrite"
