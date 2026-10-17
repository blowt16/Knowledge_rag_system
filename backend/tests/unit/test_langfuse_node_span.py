"""节点 span 的 I/O 摘要、脱敏与生命周期（【方案 §5.1 / §12.1】#5#6）。

**核心承诺**：节点 span 只带**摘要** —— query、chunk_id/文档名/页码/分数、
正文前 80 字；完整检索正文不上传（节点 state 里可能有几十条片段）。

⚠️ 上一版栽在「注释声称安全 ≠ 实际安全」（builder.py:93-97）。
这条测试就是那份承诺的凭据 —— 人工去云端翻是一次性的，测试是每次都跑的。
"""
from __future__ import annotations

import re

import pytest

from app.core import observability
from app.graph.builder import traced
from app.graph.state import Chunk
from tests.lf_capture import CapturingLangfuse

MARKER = "2023123456的缓考申请_正文标记"


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


def test_rerank_span_carries_input_output(capture):
    """非 LLM 节点也要有 input/output —— 这是 UI 面板的数据源。"""

    async def _fake_node(state):
        return {"reranked": [Chunk(chunk_id="C2", text="SECOND", score=0.91)],
                "retrieval_confidence": 0.91,
                "rerank_degraded": False,
                "trace": [{"node": "rerank", "ms": 5, "recalled": 1,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("rerank", _fake_node)({
        "query": "QUERY_MARKER",
        "candidates": [Chunk(chunk_id="C1", text="FIRST", score=0.2),
                       Chunk(chunk_id="C2", text="SECOND", score=0.3)],
    }))
    observability.get_client().flush()

    body = capture.body()
    assert "langfuse.observation.input" in body, "input 属性没发出去"
    assert "langfuse.observation.output" in body, "output 属性没发出去"
    assert "QUERY_MARKER" in body, "输入 query 没进 span"
    assert "C1" in body and "C2" in body, "候选 chunk_id 没进 span"


def test_node_io_strings_are_masked(capture):
    """新增的 input/output 摘要也要过脱敏钩子。"""
    student_id = "2023123456"

    async def _fake_node(state):
        return {"reranked": [Chunk(chunk_id="C1",
                                   text=f"学号{student_id}的申请", score=0.5)],
                "trace": [{"node": "rerank", "ms": 1, "recalled": 1,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("rerank", _fake_node)({
        "query": f"学号{student_id}怎么申请",
        "candidates": [Chunk(chunk_id="C1",
                             text=f"学号{student_id}的记录", score=0.2)],
    }))
    observability.get_client().flush()

    body = capture.body()
    assert "langfuse.observation.input" in body, "input 属性没发出去"
    assert student_id not in body, "新加的 input/output 没走脱敏"


def test_chunk_text_is_truncated_in_span(capture):
    """正文只带前 80 字 —— 完整片段不上传。"""
    head = "HEAD_MARKER"
    tail = "TAIL_MARKER"
    text = head + "x" * 200 + tail

    async def _fake_node(state):
        return {"candidates": [Chunk(chunk_id="C1", text=text, score=0.1)],
                "trace": [{"node": "retrieve", "ms": 3, "recalled": 1,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("retrieve", _fake_node)({
        "retrieval_queries": [{"text": "Q", "target": "both",
                               "weight": 1.0, "source": "verbatim"}],
    }))
    observability.get_client().flush()

    body = capture.body()
    assert head in body, "预览开头没进 span"
    assert tail not in body, "完整正文进了 span"


def test_generate_span_does_not_upload_full_context(capture):
    """上下文全文不上传 —— 只传长度与答案预览。"""
    context_marker = "CONTEXT_FULL_TEXT_MARKER"
    answer_tail = "ANSWER_TAIL_MARKER"

    async def _fake_node(state):
        return {"context": context_marker * 20,
                "answer": "ANSWER_HEAD" + "y" * 200 + answer_tail,
                "decision": "ANSWERED",
                "trace": [{"node": "generate", "ms": 12, "recalled": 3,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("generate", _fake_node)({"query": "q"}))
    observability.get_client().flush()

    body = capture.body()
    assert body, "没发出任何东西 —— 断言会假通过"
    assert context_marker not in body, "上下文全文进了 span"
    assert answer_tail not in body, "答案正文超过预览长度仍进了 span"
    assert "ANSWER_HEAD" in body, "答案预览没进 span"


def test_node_span_records_metadata(capture):
    """正控：该记的确实记了（否则上一条可能只是因为什么都没记）。"""

    async def _fake_node(state):
        return {"trace": [{"node": "retrieve", "ms": 42, "recalled": 7,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("retrieve", _fake_node)({}))
    observability.get_client().flush()

    body = capture.body()
    assert "node.retrieve" in body, "节点名没进 span"
    # ⚠️ 断言键名而不只是数字 —— 单断言 `"42" in body` 会命中 trace id / 时间戳，
    #    那是「其实什么都没记」也能通过的假绿。
    assert "rag.duration_ms" in body, "耗时字段键名没进 span"
    assert "rag.recalled" in body, "召回数字段键名没进 span"


def test_metadata_keys_stay_allowlisted(capture):
    """属性键**白名单**：新键必须显式加进来，防止有人顺手塞内容。

    ⚠️ 这条是从被取代的 `test_span_redaction.py:70` 那条白名单搬过来的。
       「正文不出现在字节里」比白名单**更强**，但盖不住一种缺口：
       新加了一个键、而它的值恰好不含测试标记 —— 白名单是那条缺口唯一的锁。
       （原测试绑死在 OTel 的属性名上，故只能重写。）
    """

    async def _fake_node(state):
        return {"trace": [{"node": "retrieve", "ms": 1, "recalled": 1,
                           "degraded": "timeout"}]}

    import asyncio
    asyncio.run(traced("retrieve", _fake_node)({}))
    observability.get_client().flush()

    body = capture.body()
    keys = set(re.findall(
        r"langfuse\.observation\.metadata\.([A-Za-z0-9_.\-]+)", body))
    # 先确认真的解析到了 —— 正则与 SDK 的属性名一旦脱节，下面那条会假通过
    assert keys, "一个 metadata 键都没解析到 —— 正则和 SDK 属性名脱节了"
    allowed = {"rag.duration_ms", "rag.recalled", "rag.degraded", "rag.error"}
    assert keys <= allowed, f"出现了白名单外的 metadata 键：{keys - allowed}"


def test_current_node_is_visible_inside_node(capture):
    """★ §12.1 修正 1：节点执行期间 `current_node()` 必须能读到节点名。

    SDK 不提供「当前观测名」（实测 obs.name 不存在），所以靠 contextvar。
    """
    seen = {}

    async def _fake_node(state):
        seen["node"] = observability.current_node()
        return {}

    import asyncio
    asyncio.run(traced("rewrite", _fake_node)({}))
    assert seen["node"] == "rewrite"


def test_current_node_resets_after_node(capture):
    """节点跑完要还原，不能让上一个节点的名字漏到下一个。"""
    import asyncio

    async def _nop(state):
        return {}

    asyncio.run(traced("route", _nop)({}))
    assert observability.current_node() == "unknown"


def test_node_exception_records_type_only(capture):
    """异常只记类型 —— message 可能回显 prompt（builder.py:90 的既有约定）。"""

    async def _boom(state):
        raise ValueError(f"上游报错，回显内容是：{MARKER}")

    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(traced("rewrite", _boom)({}))
    observability.get_client().flush()

    body = capture.body()
    assert "ValueError" in body, "异常类型没记"
    assert MARKER not in body, "异常 message 带上了正文"
