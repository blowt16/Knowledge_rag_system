"""节点 9 generate —— 流式 JSON 解析器（§8.2 M3-3 的测试点）。

代码 M0 就写好了，本轮补护栏 + 实测出的两处缺陷修掉。

四条「原方案没写、不写就编不出代码」的细节各有用例；后面两条 ★ 是本轮实测补的：

- ★ **答案收尾后有别的字段时不能把 JSON 尾巴当正文**。原实现只在
  「`"` + 空白 + `}`」时收尾，遇到 `{"answer":"…","decision":"…"}`（模型没按
  「decision 在最前」的提示词走）会把 `","decision":"ANSWERED` 这串垃圾
  一路发给用户，并写进 `answer`（cite/verify 也拿这份文本算偏移）。
- ★ **上游中断 ≠ 拒答**。原实现在流式失败时退回 `REFUSED_NO_EVIDENCE`，
  于是客户端会先收到 `error`、紧接着又收到 `refused` —— 与 §3.5.3 节点 9
  「降级」五条第 ③ 条（不发 refused）冲突，还会把「模型输出坏了」
  记成「知识库没有依据」。

为什么值得单测：解析器错了**不报错**。用户只会看到答案里混进 JSON 片段、
或者答案被截断在第一个引号上 —— 两者都不会触发任何告警。
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.graph.nodes import generate as GEN
from app.graph.state import Chunk, new_state


def _evidence(n: int = 2) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", text=f"证据{i}", document_name=f"文档{i}")
            for i in range(1, n + 1)]


def _feed(pieces: list[str]) -> GEN.StreamJsonParser:
    parser = GEN.StreamJsonParser()
    for piece in pieces:
        parser.feed(piece)
    return parser


# ============================================================
# 细节 ②：answer 何时结束
# ============================================================

def test_quote_inside_answer_is_not_treated_as_end():
    """答案正文里的引号不是结束标志（文号、引语里很常见）。"""
    parser = _feed(['{"decision":"ANSWERED","answer":"他说"好的"[1]。"}'])
    assert parser.answer_text == '他说"好的"[1]。'
    assert parser.state.finished


def test_quote_then_comma_inside_answer_still_content():
    """★ 反向锁：正文里的 `",` 不能被「收尾探测」吃掉。

    探测的判据是**逗号后面得是 `"decision"`**，不是「见到逗号就收尾」。
    """
    parser = _feed(['{"decision":"ANSWERED","answer":"他说"你好",然后[1]。"}'])
    assert parser.answer_text == '他说"你好",然后[1]。'


# ============================================================
# 细节 ①：转义还原（token 必须是解码后的纯文本）
# ============================================================

def test_escapes_are_decoded_to_plain_text():
    parser = _feed([r'{"decision":"ANSWERED","answer":"第一行\n第二行\"引号\" 中文"}'])
    assert parser.answer_text == '第一行\n第二行"引号" 中文'


def test_tokens_are_streamed_not_buffered_until_the_end():
    """逐字下发（不是攒到最后一次性给）—— 流式的意义就在这里。"""
    parser = GEN.StreamJsonParser()
    out = parser.feed('{"decision":"ANSWERED","answer":"甲乙')
    assert "".join(out) == "甲乙"


# ============================================================
# 细节 ④：decision 不在最前
# ============================================================

def test_decision_after_answer_keeps_tokens_and_drops_json_tail():
    """★ `{"answer":…,"decision":…}`：前面的 token 不能丢，JSON 尾巴不能混进来。"""
    parser = _feed(['{"answer":"你好[1]。', '","decision":"ANSWERED"}'])
    assert parser.answer_text == "你好[1]。", "JSON 尾巴混进答案了"
    assert parser.state.decision == "ANSWERED"
    assert parser.state.finished


def test_decision_after_answer_with_whitespace():
    """字段名与冒号之间有空白 —— 判据要去掉空白再比。"""
    parser = _feed(['{"answer":"你好[1]。', '", "decision" : "ANSWERED"}'])
    assert parser.answer_text == "你好[1]。"
    assert parser.state.decision == "ANSWERED"


def test_decision_missing_buffers_everything_and_finish_reports_error():
    """流结束仍未见 `decision` → 按「降级」处理（一个字符都不下发）。"""
    parser = _feed(['{"answer":"甲[1]。"}'])
    assert parser.answer_text == "" or parser.state.answer_started is False
    assert parser.finish() == ("", "", "decision 字段缺失")


# ============================================================
# 细节 ③：降级降成什么（五条）
# ============================================================

async def test_stream_error_keeps_partial_tokens_and_emits_error(monkeypatch):
    """① 停 token ② 发 error{upstream_error} ③ 不发 refused ④ 保留已流出 ⑤ 不重试。"""
    calls = {"n": 0}

    async def fake_stream(messages, **kw):
        calls["n"] += 1
        yield '{"decision":"ANSWERED","answer":"已经流出的内容[1]。'
        raise RuntimeError("上游断了")

    monkeypatch.setattr(llm, "stream_raw", fake_stream)
    events: list[dict] = []

    decision, answer, error = await GEN._stream_generate("提示词", events.append, 5)

    assert calls["n"] == 1, "不自动重试"
    assert answer == "已经流出的内容[1]。", "已流出的 token 必须保留"
    # token 是**逐字**下发的（每个字符一个事件），所以这里比拼接结果
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == "已经流出的内容[1]。"
    assert events[-1]["type"] == "error" and events[-1]["code"] == "upstream_error"
    assert not any(e["type"] == "refused" for e in events)
    assert error and not decision


async def test_refusal_never_streams_the_model_text(monkeypatch):
    """★ 评审抓出：REFUSED 路径**不发模型文案**（§3.5.3 节点 9）。

    否则用户先看到模型的自由发挥（气泡里），下面再叠一个服务端拒答框 ——
    正是规格要避免的「夹带解释或猜测」；而且刷新后文本又变了个样
    （落库的是服务端固定话术）。
    """
    async def fake_stream(messages, **kw):
        yield '{"decision":"REFUSED_NO_EVIDENCE","answer":"我认为这个问题不便回答。"}'

    monkeypatch.setattr(llm, "stream_raw", fake_stream)
    events: list[dict] = []

    decision, answer, error = await GEN._stream_generate("提示词", events.append, 5)

    assert not [e for e in events if e["type"] == "token"], "拒答时一个 token 都不该下发"
    assert decision == "REFUSED_NO_EVIDENCE"
    assert answer == GEN.REFUSAL_TEXT
    assert error is None


async def test_first_char_check_failure_degrades_immediately(monkeypatch):
    """首字符校验：去掉 BOM/空白/围栏后不以 `{` 开头 → 立即中止（一个 token 都不发）。"""
    async def fake_stream(messages, **kw):
        yield "抱歉，我不能回答这个问题。"

    monkeypatch.setattr(llm, "stream_raw", fake_stream)
    events: list[dict] = []

    await GEN._stream_generate("提示词", events.append, 5)

    assert [e["type"] for e in events] == ["error"]


@pytest.mark.parametrize("text,expected", [
    ("  ﻿```json\n{\"decision\"", True),   # 围栏 + BOM + 空白 → 剥掉后是 {
    ("   ", True),                               # 还没收到有效字符，不算失败
    ("抱歉，我无法回答", False),
    ("{", True),
    # 评审抓出：围栏被切成两片时（首片只到 "```" 或 "```json"），还判断不了，
    # 不能判失败 —— 那会把一条正常的流当场中止
    ("```", True),
    ("```json", True),
    ("```json\n", True),
])
def test_precheck_prefix(text, expected):
    assert GEN.StreamJsonParser.precheck_prefix(text) is expected


async def test_generate_node_degrades_instead_of_refusing(monkeypatch):
    """★ 流式降级后**不能**退化成拒答（`refused` 必须为 False）。"""
    async def fake_stream(messages, **kw):
        yield '{"decision":"ANSWERED","answer":"半截[1]。'
        raise RuntimeError("boom")

    monkeypatch.setattr(llm, "stream_raw", fake_stream)
    monkeypatch.setattr(GEN, "_stream_writer", lambda: (lambda evt: None))

    out = await GEN.generate_node(new_state(
        query="缓考怎么申请？", resolved_query="缓考怎么申请？",
        context="[1] 证据", evidence=_evidence(),
    ))

    assert out["refused"] is False
    assert out["refusal_reason"] == ""
    assert out["answer"] == "半截[1]。"
    assert out["decision"] == ""
    # 兜底必须留痕，否则评测护栏看不出这一轮是坏的
    assert out["trace"][0]["degraded"] == "unavailable"


async def test_tail_read_failure_after_a_complete_answer_is_not_a_degradation(monkeypatch):
    """★ 答案已完整之后的读取失败，只该丢 usage —— 不能把这一轮降级。

    ⚠️ 这个窗口是 A4/C3 的修复带来的：要拿到尾部那个带 `usage` 的 chunk
       （`choices` 是空数组，见 `llm.stream_raw`），消费方必须把流**读完**，
       于是读循环会越过答案的收尾 `}`。上游偏偏在这之后断流时，异常就落在
       「用户早已拿到完整答案」的时刻 —— 再补一个「生成中断，请重试」
       会把一条好答案打成降级（终审发现的收尾读取缺陷）。

    判据用解析器自己的 `answer_done`（它只表示答案正文已收尾），不另造信号。
    走**真实消费方**（`generate_node` → `_stream_generate` → `llm.stream_raw`），
    断流点就在尾部 usage chunk 之后。
    """
    import json

    import httpx

    content = '{"decision":"ANSWERED","answer":"图书馆每天 8:00 至 22:00 开放[1]。"}'
    lines = (
        [f'data: {json.dumps({"choices": [{"delta": {"content": c}}]})}'
         for c in [content[i:i + 7] for i in range(0, len(content), 7)]]
        # 与真实上游同款：带 usage 的 chunk 在内容**之后**，`choices` 为空数组
        + [f'data: {json.dumps({"choices": [], "usage": _USAGE})}']
    )

    class _SSE:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line
            raise RuntimeError("上游在答案之后断流")

        async def aread(self):
            return b""

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, *a, **k):
            class _Ctx:
                async def __aenter__(self_inner): return _SSE()
                async def __aexit__(self_inner, *a): return False
            return _Ctx()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    events: list[dict] = []
    monkeypatch.setattr(GEN, "_stream_writer", lambda: events.append)

    out = await GEN.generate_node(new_state(
        query="图书馆几点开门？", resolved_query="图书馆几点开门？",
        context="[1] 图书馆每天 8:00 至 22:00 开放。", evidence=_evidence(),
    ))

    assert out["answer"] == "图书馆每天 8:00 至 22:00 开放[1]。", "完整的答案被收尾失败吃掉了"
    assert out["decision"] == "ANSWERED"
    assert out["refused"] is False
    assert out["trace"][0]["degraded"] is None, "答案早已完整，这一轮不该被标降级"
    assert not [e for e in events if e["type"] == "error"], "不该给用户弹「生成中断，请重试」"
    assert "".join(e["text"] for e in events if e["type"] == "token") == out["answer"]


#: 尾部 usage chunk 的形状（与 `test_langfuse_generation.py` 同款）
_USAGE = {"prompt_tokens": 54, "completion_tokens": 12}


# ============================================================
# 节点级：拒答路径与标记校验（未改动的既有行为，一并钉住）
# ============================================================

async def test_refused_decision_uses_server_text(monkeypatch):
    """REFUSED_NO_EVIDENCE → 忽略模型文案，用服务端固定话术。"""
    parser = _feed(['{"decision":"REFUSED_NO_EVIDENCE","answer":"模型自己写的解释"}'])
    decision, answer, error = parser.finish()
    assert decision == "REFUSED_NO_EVIDENCE"
    assert answer == GEN.REFUSAL_TEXT
    assert error is None


async def test_answered_with_empty_body_is_an_error():
    parser = _feed(['{"decision":"ANSWERED","answer":""}'])
    assert parser.finish() == ("ANSWERED", "", "ANSWERED 但正文为空")


async def test_answered_without_valid_marker_falls_back_to_refusal(monkeypatch):
    """服务端确定性校验：ANSWERED 但正文一个合法标记都没有 → 拒答。"""
    async def fake_json(messages, **kw):
        return {"decision": "ANSWERED", "answer": "没有任何标记的答案。"}

    monkeypatch.setattr(llm, "complete_json", fake_json)

    out = await GEN.generate_node(new_state(
        query="q", resolved_query="q", context="[1] 证据", evidence=_evidence(),
    ))

    assert out["decision"] == "REFUSED_NO_EVIDENCE"
    assert out["refused"] is True


def test_extract_markers_is_the_only_source_of_numbers():
    assert GEN.extract_markers("结论甲[1]。结论乙[2][3]。", evidence_count=5) == [1, 2, 3]
    assert GEN.extract_markers("没有标记", evidence_count=5) == []
