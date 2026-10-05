"""两条拒答路径在 SSE 上的区别（§7.2 M2-5 的测试点）。

**两条路很容易实现成一样，但前端要靠它们区分状态**：

| 拒答来源 | `decision` 事件 | `refused` 事件 |
|---|---|---|
| 候选为空（`rerank` 后短路 → 节点 11） | **不发送** | 发送，`reason="no_candidate"` |
| 模型判定证据不足（节点 9 → 直接 END） | 发送 `REFUSED_NO_EVIDENCE` | 发送，`reason="insufficient_evidence"` |

即 **`refused` 两条都发；`decision` 只在 `generate` 真跑过之后才发** ——
`decision` 是 `generate` 的产物，没跑过就没有这个状态可言。

⚠️ 拒答文案必须**由服务端给出**（`refused` 载荷里的 `text`）：
   前端按 reason 自造文案的话，两条路的话术会各写一份、迟早不一致。

⚠️ 这里用**假图**驱动 `stream_chat`：本用例考的是 SSE 事件的组装与顺序，
   不是图本身。真图那条链路由 `test_graph_trace.py` 与 M2 评测器覆盖。
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from app import db
from app.core.deps import UserContext
from app.services import chat_service
from app.services.conversation_service import create_conversation
from tests.support import delete_admin, insert_admin

KNOWLEDGE_TRACE = [{"node": "route", "ms": 1, "recalled": 0, "degraded": None},
                   {"node": "retrieve", "ms": 1, "recalled": 0, "degraded": None}]


class FakeGraph:
    """只回放一个最终状态 —— 让 SSE 层的行为可单独断言。"""

    def __init__(self, final: dict):
        self._final = final

    async def astream(self, state, stream_mode=None):
        yield ("values", self._final)


@pytest_asyncio.fixture
async def session():
    admin_id = await insert_admin()
    sid = f"t_{uuid.uuid4().hex[:12]}"
    await create_conversation(sid, admin_id, title="拒答路径测试")
    try:
        yield sid, admin_id
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM qa_logs WHERE session_id = $1", sid)
            await conn.execute("DELETE FROM messages WHERE conversation_id = $1", sid)
            await conn.execute("DELETE FROM conversations WHERE id = $1", sid)
        await delete_admin(admin_id)


async def _frames(final: dict, sid: str) -> list[tuple[str, dict]]:
    """跑一遍 stream_chat，把 SSE 文本帧解析成 (event, data)。"""
    frames: list[tuple[str, dict]] = []
    stream = chat_service.stream_chat(
        query="库里有据的问题",
        session_id=sid,
        is_new_session=False,
        include_restricted=False,
        user=UserContext(id="u", username="u", role="student", token_version=0),
        is_disconnected=_never_disconnected,
    )
    async for frame in stream:
        event = frame.split("\n", 1)[0].removeprefix("event: ")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        frames.append((event, data))
    return frames


async def _never_disconnected() -> bool:
    return False


def _events(frames) -> list[str]:
    return [e for e, _ in frames]


@pytest_asyncio.fixture(autouse=True)
def _fake_graph(monkeypatch):
    holder: dict = {}

    def install(final):
        holder["final"] = final
        monkeypatch.setattr(chat_service, "get_graph",
                            lambda: FakeGraph(holder["final"]))

    yield install
    monkeypatch.undo()


async def test_no_candidate_path_sends_refused_without_decision(session, _fake_graph):
    sid, _ = session
    _fake_graph({
        "route": "knowledge",
        "trace": KNOWLEDGE_TRACE + [{"node": "rerank", "ms": 0, "recalled": 0, "degraded": None},
                                    {"node": "refuse", "ms": 0, "recalled": 0, "degraded": None}],
        "refused": True,
        "refusal_reason": "no_candidate",
        "answer": "知识库中未找到相关依据，无法回答该问题。",
        "decision": "",           # 这条路径**不产生** decision
        "reranked": [],
    })

    frames = await _frames({}, sid)
    events = _events(frames)

    assert "refused" in events, "两条拒答路径都必须发 refused"
    assert "decision" not in events, "候选为空这条路**不发** decision（generate 没跑过）"
    assert "citations" not in events

    refused = dict(frames)["refused"]
    assert refused["reason"] == "no_candidate"
    assert refused["text"].strip(), "拒答文案必须由服务端给出，前端不得自造"
    assert refused.get("hint")


async def test_insufficient_evidence_path_sends_both(session, _fake_graph):
    sid, _ = session
    _fake_graph({
        "route": "knowledge",
        "trace": KNOWLEDGE_TRACE + [{"node": "generate", "ms": 1, "recalled": 0, "degraded": None}],
        "refused": True,
        "refusal_reason": "insufficient_evidence",
        "decision": "REFUSED_NO_EVIDENCE",
        "answer": "依据不足，无法回答。",
        "citations": [],
    })

    frames = await _frames({}, sid)
    events = _events(frames)

    assert "decision" in events, "证据不足这条路**要发** decision"
    assert dict(frames)["decision"]["decision"] == "REFUSED_NO_EVIDENCE"
    assert dict(frames)["refused"]["reason"] == "insufficient_evidence"
    # 顺序：decision 在 refused 之前
    assert events.index("decision") < events.index("refused")


async def test_answered_path_sends_citations_and_verify(session, _fake_graph):
    sid, _ = session
    _fake_graph({
        "route": "knowledge",
        "trace": KNOWLEDGE_TRACE + [{"node": "generate", "ms": 1, "recalled": 0, "degraded": None},
                                    {"node": "cite", "ms": 1, "recalled": 0, "degraded": None}],
        "refused": False,
        "decision": "ANSWERED",
        "answer": "答案是……[1]",
        "citations": [],
        "verify_report": {"total_claims": 1, "supported_claims": 1,
                          "uncited_claims": [], "invalid_markers": [], "passed": True},
    })

    frames = await _frames({}, sid)
    events = _events(frames)

    assert "decision" in events and "citations" in events and "verify" in events
    assert "refused" not in events
    assert dict(frames)["decision"]["decision"] == "ANSWERED"


async def test_chat_route_never_sends_citations(session, _fake_graph):
    """chat 分支不检索、不引用 —— 发 citations 会让前端显示不存在的证据。"""
    sid, _ = session
    _fake_graph({
        "route": "chat",
        "trace": [{"node": "route", "ms": 1, "recalled": 0, "degraded": None},
                  {"node": "chat", "ms": 1, "recalled": 0, "degraded": None}],
        "answer": "你好！",
        "decision": "ANSWERED",
    })

    frames = await _frames({}, sid)
    events = _events(frames)

    assert "citations" not in events and "verify" not in events and "refused" not in events
