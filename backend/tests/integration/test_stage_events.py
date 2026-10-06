"""SSE `stage` 事件（§3.7.2 / §4.2.4.2 文案映射表）。

⚠️ **为什么到 M4 才补**：契约（`StageName` / `StageEvent`）M0 就冻住了，
   但**发送端从来没实现过**（grep 全后端零命中）—— 前端的阶段提示因此无源可驱动。
   本用例锁的就是「服务端到底发不发、按什么顺序发」。

⚠️ **`stage` 与图内部节点名是解耦的**（§3.7.2）：节点重构不该改 SSE 协议。
   所以映射表只有一份（`chat_service.NODE_TO_STAGE`），这里是它的行为锁。

⚠️ 与既有事件顺序的关系：`stage` 是**追加**事件，不改变
   `route/resolved/token/decision/citations/verify/refused/done` 的相对顺序 ——
   `test_refusal_paths.py` 仍是那半边权威。

⚠️ **`clarify` 与 `refuse` 刻意不发 stage**：
   - `clarify` 之后用户看到的是一句反问，不是答案 —— 发「正在组织答案…」是骗人；
   - `refuse` 是 `rerank` 后零成本短路，没有可提示的耗时。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import db
from app.core.deps import UserContext
from app.services import chat_service
from app.services.conversation_service import create_conversation
from tests.support import delete_admin, insert_admin

# 图上跑出来的是 Chunk 对象（不是 dict）—— `_persist` 直接取 `.chunk_id`
RERANKED = [SimpleNamespace(chunk_id="c1")]


class ScriptedGraph:
    """按脚本回放 (mode, payload) —— stage 的触发源是 debug 通道。"""

    def __init__(self, script: list[tuple[str, dict]]):
        self._script = script

    async def astream(self, state, stream_mode=None):
        for item in self._script:
            yield item


def task_start(name: str) -> tuple[str, dict]:
    """真图上 `stream_mode="debug"` 在**节点开工那一刻**产出的事件。"""
    return ("debug", {"type": "task", "payload": {"id": f"{name}-1", "name": name,
                                                  "input": {}}})


def task_result(name: str) -> tuple[str, dict]:
    return ("debug", {"type": "task_result", "payload": {"id": f"{name}-1", "name": name,
                                                         "result": {}}})


def script_for(nodes: list[str], final: dict, extra: list | None = None
               ) -> list[tuple[str, dict]]:
    """一个节点 = 开工 + 结束 + 一个状态快照，与真图的事件形状一致。"""
    script: list[tuple[str, dict]] = []
    for name in nodes:
        script.append(task_start(name))
        if extra and name in extra:
            script.extend(extra[name])
        script.append(task_result(name))
        script.append(("values", final))
    return script


@pytest_asyncio.fixture
async def session():
    admin_id = await insert_admin()
    sid = f"t_{uuid.uuid4().hex[:12]}"
    await create_conversation(sid, admin_id, title="stage 事件测试")
    try:
        yield sid
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM qa_logs WHERE session_id = $1", sid)
            await conn.execute("DELETE FROM messages WHERE conversation_id = $1", sid)
            await conn.execute("DELETE FROM conversations WHERE id = $1", sid)
        await delete_admin(admin_id)


async def _frames(sid: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    async for frame in chat_service.stream_chat(
        query="库里有据的问题",
        session_id=sid,
        is_new_session=False,
        include_restricted=False,
        user=UserContext(id="u", username="u", role="student", token_version=0),
        is_disconnected=_never_disconnected,
    ):
        event = frame.split("\n", 1)[0].removeprefix("event: ")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        frames.append((event, data))
    return frames


async def _never_disconnected() -> bool:
    return False


def _stages(frames: list[tuple[str, dict]]) -> list[str]:
    return [d["stage"] for e, d in frames if e == "stage"]


@pytest_asyncio.fixture(autouse=True)
def _install(monkeypatch):
    def install(script: list[tuple[str, dict]]):
        monkeypatch.setattr(chat_service, "get_graph", lambda: ScriptedGraph(script))

    yield install
    monkeypatch.undo()


KNOWLEDGE_NODES = ["resolve", "route", "rewrite", "retrieve", "rerank",
                   "build_context", "generate", "cite"]


async def test_knowledge_path_stage_sequence(session, _install):
    """知识型问答的完整阶段序列 —— 前端就是按这个序列切换提示文案的。"""
    final = {"route": "knowledge", "refused": False, "decision": "ANSWERED",
             "answer": "答案是……[1]", "citations": [], "reranked": RERANKED}
    _install(script_for(KNOWLEDGE_NODES, final))

    frames = await _frames(session)

    assert _stages(frames) == ["resolving", "routing", "retrieving",
                               "reranking", "generating", "verifying"]
    # 载荷只有这两个字段（§3.7.2 的 StageEvent）
    assert set(dict(frames)["stage"].keys()) == {"stage", "label"}


async def test_consecutive_same_stage_emitted_once(session, _install):
    """`build_context` 与 `generate` 都归 `generating` —— 只能发一次。

    连发两次会让前端的提示闪一下再回到同一句，且没有任何信息量。
    """
    nodes = ["resolve", "route", "rewrite", "retrieve", "rerank",
             "build_context", "generate"]
    final = {"route": "knowledge", "refused": False, "decision": "ANSWERED",
             "answer": "答案", "citations": [], "reranked": RERANKED}
    _install(script_for(nodes, final))

    stages = _stages(await _frames(session))

    assert stages.count("generating") == 1, f"generating 只该发一次，实为 {stages}"


async def test_stage_arrives_before_first_token(session, _install):
    """★ 阶段提示的**唯一用途**是填充首字到达前的静默期（§4.2.4.2）。

    若 `generating` 排在了第一个 token 之后，这个事件就等于不存在 ——
    静默期已经结束了。
    """
    final = {"route": "knowledge", "refused": False, "decision": "ANSWERED",
             "answer": "你好呀", "citations": [], "reranked": RERANKED}
    script = script_for(
        ["resolve", "route", "rewrite", "retrieve", "rerank", "build_context"],
        final,
    )
    script += [
        task_start("generate"),
        ("custom", {"type": "token", "text": "你"}),
        task_result("generate"),
        ("values", final),
        ("debug", {"type": "task", "payload": {"name": "cite"}}),
        ("values", final),
    ]
    _install(script)

    frames = await _frames(session)
    events = [e for e, _ in frames]

    assert "stage" in events and "token" in events
    first_token = events.index("token")
    # ⚠️ 只看**首个 token 之前**的那一段：`verifying` 本来就在生成之后才到
    #    （§3.7.2 的取值表），拿它去比「必须早于 token」是错的断言。
    stages_before = [d["stage"] for e, d in frames[:first_token] if e == "stage"]
    assert stages_before and stages_before[-1] == "generating", \
        f"首个 token 之前必须已经发过 generating，实为 {stages_before}"


async def test_chat_route_gets_generating_only(session, _install):
    """闲聊分支不检索 —— 只该有 resolving / routing / generating 三段。"""
    final = {"route": "chat", "refused": False, "decision": "ANSWERED", "answer": "你好！"}
    _install(script_for(["resolve", "route", "chat"], final))

    assert _stages(await _frames(session)) == ["resolving", "routing", "generating"]


async def test_clarify_route_emits_no_generating(session, _install):
    """★ 澄清分支**刻意不发** `generating`。

    澄清之后用户看到的是一句反问、不是答案 —— 发「正在组织答案…」是把
    「马上要反问你」说成「马上给你答案」，比不发更糟。
    """
    final = {"route": "clarify", "clarify_facets": ["缓考", "补考"], "refused": False}
    _install(script_for(["resolve", "route", "clarify"], final))

    assert _stages(await _frames(session)) == ["resolving", "routing"]


async def test_refuse_path_stops_at_reranking(session, _install):
    """候选为空短路到 `refuse` —— 没有 generating / verifying。"""
    final = {"route": "knowledge", "refused": True, "refusal_reason": "no_candidate",
             "answer": "知识库中未找到相关依据，无法回答该问题。", "reranked": []}
    _install(script_for(["resolve", "route", "rewrite", "retrieve", "rerank", "refuse"],
                        final))

    assert _stages(await _frames(session)) == ["resolving", "routing", "retrieving",
                                               "reranking"]


async def test_no_stage_after_error(session, _install):
    """`error` 是**终止事件**（§3.7.2）—— 之后不该再冒出新的阶段提示。

    「正在核对引用…」跟在「生成中断，请重试」后面出现，等于告诉用户
    这轮还在正常往下走。
    """
    final = {"route": "knowledge", "refused": False, "decision": "",
             "answer": "已经流出的半截正文", "citations": [], "reranked": RERANKED}
    script = [
        task_start("resolve"),
        ("values", final),
        task_start("generate"),
        ("custom", {"type": "error", "code": "upstream_error",
                    "message": "生成中断，请重试"}),
        task_start("cite"),          # 极端情况下后面还有节点在跑
        ("values", final),
    ]
    _install(script)

    frames = await _frames(session)
    events = [e for e, _ in frames]

    assert "error" in events
    stages = _stages(frames)
    assert stages == ["resolving", "generating"], \
        f"error 之前照常发，之后一律停：实为 {stages}"


async def test_unknown_node_and_malformed_payload_do_not_emit(session, _install):
    """协议要能容忍：图里新增节点、debug 载荷形状变化，都不能炸。

    图是会继续长的（M5 还要加评测链路），一个映射表里没有的节点名
    不该让整条 SSE 流挂掉。
    """
    final = {"route": "knowledge", "refused": False, "decision": "ANSWERED",
             "answer": "答案", "citations": [], "reranked": RERANKED}
    script = [
        task_start("resolve"),                                # 已知节点：必须发
        ("debug", {"type": "task", "payload": {"name": "brand_new_node"}}),
        ("debug", {"type": "task", "payload": {}}),           # 没有 name
        ("debug", {"type": "task"}),                          # 没有 payload
        ("debug", {"type": "task_result", "payload": {"name": "resolve"}}),  # 不是开工
        ("values", final),
    ]
    _install(script)

    frames = await _frames(session)

    assert _stages(frames) == ["resolving"], "已知节点照常发，其余一律安静跳过"
    assert "done" in [e for e, _ in frames], "未知节点不能让整条流中断"
