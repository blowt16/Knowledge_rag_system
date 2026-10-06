"""span 脱敏护栏（§3.2.3.2）—— **属性里不许出现正文**。

为什么要有这条锁：
    日志那边脱敏做了（`test_logging_redaction`），但 span 是**另一个存储**
    （Jaeger）。自动埋点（`opentelemetry-instrumentation-langchain`）默认就把
    prompt 与模型返回写进 span 属性 —— 后果是「日志干净了，Jaeger 里却摆着
    学号姓名成绩原文」。本项目因此**不引自动埋点**，改手工埋点（见
    `builder.traced`），这条测试就是手工埋点那份承诺的凭据。

    人工去 Jaeger 翻属性是**一次性**的；这条测试是**每次都跑**的。
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import telemetry
from app.graph.builder import traced

#: 节点必然产出、也**必然不许进 span** 的东西（用户提问 / 检索片段 / 答案正文）
MARKER = "2023123456的缓考申请_正文标记"


@pytest.fixture()
def captured(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "get_tracer",
                        lambda name="campus-rag": provider.get_tracer(name))
    return exporter


def _all_attribute_text(spans) -> str:
    parts: list[str] = []
    for span in spans:
        for key, value in (span.attributes or {}).items():
            parts.append(f"{key}={value}")
    return "\n".join(parts)


async def test_node_span_attributes_contain_no_content(captured):
    """节点输入里有学号、输出里有正文 —— 两者都不许出现在 span 属性里。"""

    async def _fake_node(state):
        # 真实的节点会读 state["query"] 并写答案；这里原样返回一段带标记的内容
        return {"answer": MARKER, "context": MARKER,
                "trace": [{"node": "generate", "ms": 12, "recalled": 3,
                           "degraded": None}]}

    wrapped = traced("generate", _fake_node)
    await wrapped({"query": MARKER, "session_id": "s1"})

    spans = captured.get_finished_spans()
    assert len(spans) == 1
    dump = _all_attribute_text(spans)

    assert MARKER not in dump, f"span 属性里出现了正文：\n{dump}"

    # 正控：该记的确实记了（否则上面那条断言可能只是因为什么都没记）
    attrs = spans[0].attributes
    assert attrs.get("rag.node") == "generate"
    assert attrs.get("rag.duration_ms") == 12
    assert attrs.get("rag.recalled") == 3


async def test_only_allowlisted_attribute_keys(captured):
    """属性键**白名单**：新增属性必须显式加进来，防止有人顺手塞内容。"""

    async def _fake_node(state):
        return {"trace": [{"node": "retrieve", "ms": 1, "recalled": 0,
                           "degraded": "timeout"}]}

    await traced("retrieve", _fake_node)({})

    allowed = {"rag.node", "rag.duration_ms", "rag.recalled", "rag.degraded",
               "rag.error"}
    attrs = captured.get_finished_spans()[0].attributes
    assert set(attrs) <= allowed, f"出现了白名单外的属性：{set(attrs) - allowed}"


async def test_exception_records_type_only_not_message(captured):
    """异常只记**类型**：message 可能回显 prompt，`record_exception` 会把它写进去。"""

    async def _boom(state):
        raise ValueError(f"上游报错，回显内容是：{MARKER}")

    with pytest.raises(ValueError):
        await traced("rewrite", _boom)({})

    spans = captured.get_finished_spans()
    attrs = spans[0].attributes
    assert attrs.get("rag.error") == "ValueError"
    assert MARKER not in _all_attribute_text(spans)

    # 事件（exception event）里也不许有 —— record_exception 就是往这儿写
    events = getattr(spans[0], "events", ()) or ()
    event_dump = "\n".join(f"{e.name}={e.attributes}" for e in events)
    assert MARKER not in event_dump, f"异常事件里带上了正文：{event_dump}"
