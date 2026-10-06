"""节点 9：generate —— 决策与生成（合并）（§3.5.3 节点 9）。

**证据充分性判定已并入本节点**，与生成合并为**一次结构化调用**：
原设计分两步需要两次模型调用；合并后只需一次，且判定与生成面对同一份证据，
不会出现「判定说够、生成说不够」的错位。

**代价要如实说明**：合并意味着**验证器与生成器共享同一套认知** ——
模型若误判「证据够用」，它不会在生成时自我纠正。这是**自验证困境**（§3.5.4），
**无法靠提示词消除**，只能靠分层约束控制。

结构化输出契约（字段顺序固定，便于前端增量解析）：

    {"decision":"ANSWERED","answer":"转专业需满足以下条件[1]……"}

⚠️ **无 `citation_numbers`**：引用编号由服务端从 `answer` 正文的 `[n]` 标记派生 ——
   同一信息编码两遍会互相矛盾，且能绕过校验（模型给出 `[1]` 数组但正文一个标记没写，
   服务端校验通过、`cite` 却解析出空引用）。**以正文为准**，
   校验的才是真正会被渲染的东西。

REFUSED 路径：**不流式输出模型文案，服务端使用固定拒答话术** ——
避免模型在拒答时夹带解释或猜测。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.services import context_service
from app.services.context_service import count_tokens, hard_prompt_limit

logger = logging.getLogger(__name__)

REFUSAL_TEXT = "知识库中未找到相关依据，无法回答该问题。"
REFUSAL_HINT = "建议咨询教务处或相关职能部门确认。"

# 结论句定义（生成与校验**共用同一套**，见 §3.5.3 节点 9）
_SENTENCE_SPLIT = re.compile(r"[。！？\n]")
# 过渡句 / 元陈述 —— 不算结论句
_TRANSITION = ("综上", "因此", "接下来", "首先", "其次", "最后", "总之", "另外")



# ============================================================
# 流式 JSON 解析器
# ============================================================

@dataclass
class StreamParseState:
    decision: str = ""
    answer: str = ""
    buffering: bool = True          # decision 未到时先缓冲，不下发
    answer_started: bool = False
    # 答案字符串已收尾（见到 `"` + 空白 + `}` 或 `"` + 空白 + `,` + 别的字段）。
    # 与 `finished` 分开：answer 收尾 ≠ 整条流结束 —— decision 可能还在后面，
    # 没收到 decision 就断流要按「降级」处理（见下）。
    answer_done: bool = False
    finished: bool = False
    error: str | None = None
    pending: str = field(default="")  # 尾部回退缓冲


# 答案字符串收尾后，JSON 里还能接的字段名（契约里只有 decision 一个）。
# 用「去掉空白后逐字符比对前缀」判断，这样 `, "decision" :` 这种带空白的写法也算。
_AFTER_ANSWER = ',"decision":'


def _probe_status(probe: str) -> str:
    """按住中的 `,` + 后续字符，判断它是不是「答案收尾 + 下一个字段名」。

    返回 `end`（确认收尾）/ `keep`（还看不出来，继续按住）/ `content`（是正文）。
    """
    skeleton = "".join(probe.split())
    if skeleton.startswith(_AFTER_ANSWER):
        return "end"
    if _AFTER_ANSWER.startswith(skeleton):
        return "keep"
    return "content"


class StreamJsonParser:
    """把模型输出的 JSON 流，边收边解析成 (decision, answer 增量)。

    四条原方案没写、但不写就编不出代码的细节：

    | 细节 | 规格 |
    |---|---|
    | **转义怎么还原** | `token` 必须发**解码后**的纯文本：收到 `\\"` 发 `"`、`\\n` 发换行、`\\uXXXX` 发对应字符。**不能原样透传** —— 否则用户在答案里看到反斜杠 |
    | **`answer` 何时结束** | **不能见到 `"` 就当结束** —— 答案正文里可能有引号（文号、引语）。以「**尾部 `"` + 可选空白 + `}`**」为结束标志，且**回退缓冲 N 个字符**（N ≥ 8）后才提交 |
    | **「降级」降成什么** | ① 停止下发 `token` ② 发 `error`（`code="upstream_error"`）③ **不发 `refused`**（这是上游格式错误，不是拒答）④ **保留已流出的 token** ⑤ **不自动重试** |
    | **`decision` 不在最前怎么办** | 提示词要求 `decision` 是第一个字段，但**服务端不能假设**。做法：`decision` 到达前收到的 token **先缓冲、不下发**；若流结束仍未见 `decision`，按下一条的「降级」处理 |
    | **`"` 后面是 `,` 怎么办** | 两种都合法：① 答案到此结束、后面还有别的字段（`decision` 可能在后）② 正文里的裸引号恰好后跟逗号。**不能靠一个字符下结论** —— 先按住，看逗号后面是不是 `"decision"`；是 → 答案收尾（这段 JSON 尾巴**不能**当正文发出去），不是 → 回退补发。2026-10-05 实测：没有这条时，`{"answer":"…","decision":"…"}` 的答案里会混进 `","decision":"ANSWERED` 这串垃圾 |
    """

    LOOKBACK = 12          # ≥ 8
    # 「`"` 后面是 `,`」的观察窗口上限：超过就认定是正文，不再按住
    PROBE_MAX = 40
    _DECISION_RE = re.compile(r'"decision"\s*:\s*"([A-Z_]+)"')

    _UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}

    def __init__(self, field: str = "answer", *, require_decision: bool = True) -> None:
        """`field` 是要流式下发的字段名。

        ⚠️ 节点 4（clarify）**复用本解析器**，只把字段名换成 `question`
           （`facets` 是数组、不流式，随 `route` 事件整体下发）。

        ⚠️ `require_decision` 必须可关：节点 9 的契约里有 `decision` 字段，
           而 **clarify 的 JSON 只有 facets + question、没有 decision** ——
           若沿用「未见到 decision 就一个字符都不下发」的规则，
           clarify 会**永远解析不出任何内容**。
           2026-10-05 实测踩过：表现为澄清只发出 1 个 token（走兜底补发）而非真流式。
        """
        self.field = field
        self.require_decision = require_decision
        self.state = StreamParseState()
        self._raw = ""             # 完整原始输出（结束时严格解析用）
        self._escape = False
        self._answer_chars: list[str] = []

    @property
    def _answer_key(self) -> str:
        return f'"{self.field}"'

    # ---- 首字符校验（服务端行为第 4 条）-----------------------------
    @staticmethod
    def precheck_prefix(text: str) -> bool:
        """去掉 BOM / 前导空白 / ```json 围栏后仍不以 `{` 开头 → 立即中止。

        ⚠️ BOM 与空白要**一起剥**，不能先剥 BOM 再 strip 空白 ——
           实测 `"  \\ufeff```json\\n{…"`（空白在前、BOM 在后）按原来的顺序
           剥不掉 BOM，会让一条本来正常的流被误判成格式错误而中止。
        """
        cleaned = text.lstrip("﻿ \t\r\n").strip()
        if cleaned.startswith("```"):
            # 围栏还没收完（首片可能是 "```" 或 "```json" 这种半截）→ 还判断不了，
            # 不能判失败：那会把一条正常的流当场中止（实测 precheck_prefix('```') 曾返回 False）
            if "\n" not in cleaned:
                return True
            cleaned = cleaned.split("\n", 1)[1].strip()
        if not cleaned:
            return True       # 还没收到有效字符，不算失败
        return cleaned.startswith("{")

    # ---- 增量喂入 ---------------------------------------------------
    def feed(self, piece: str) -> list[str]:
        """喂入一段模型输出，返回本次应下发的**已解码纯文本**片段列表。"""
        self._raw += piece
        if self.state.error:
            return []

        if self.require_decision and not self.state.decision:
            match = self._DECISION_RE.search(self._raw)
            if match:
                self.state.decision = match.group(1)
            elif not self.state.answer_started:
                # decision 未到、答案也还没开始 —— 先缓冲、不下发（不假设字段顺序）
                return []

        out = self._consume_answer(piece)
        # 答案已收尾 + decision 已到手 = 整条流可以停了
        if self.state.answer_done and (self.state.decision or not self.require_decision):
            self.state.finished = True
        return out

    def _consume_answer(self, piece: str) -> list[str]:
        # 定位 answer 键
        if not self.state.answer_started:
            key_at = self._raw.find(self._answer_key)
            if key_at == -1:
                return []
            colon = self._raw.find(":", key_at + len(self._answer_key))
            if colon == -1:
                return []
            quote = self._raw.find('"', colon)
            if quote == -1:
                return []
            self.state.answer_started = True
            # 把引号之后的已有内容补齐进缓冲
            self._pending_extra = self._raw[quote + 1:]

        chunk = piece if not hasattr(self, "_pending_extra") else \
            self._pending_extra + ""
        if hasattr(self, "_pending_extra"):
            del self._pending_extra
        else:
            chunk = piece

        out: list[str] = []
        for ch in chunk:
            if self.state.answer_done:
                # 答案已收尾，后面是 JSON 结构（可能还在等 decision），一个字符都不发
                break

            if getattr(self, "_probing", False):
                # 按住的 `"` + 空白 + `,`，看后面是不是别的字段名
                self._probe += ch
                status = _probe_status(self._probe)
                if status == "keep" and len(self._probe) <= self.PROBE_MAX:
                    continue
                self._probing = False
                head, probe = getattr(self, "_probe_head", ""), self._probe
                self._probe_head = self._probe = ""
                if status == "end":
                    # 答案到此为止 —— 这段 JSON 尾巴绝不能当正文发出去
                    self.state.answer_done = True
                    break
                # 是正文里的引号：连同按住的头部一起补发
                for tch in head + probe:
                    self._answer_chars.append(tch)
                    out.append(tch)
                continue

            if self._escape:
                self._escape = False
                decoded = self._UNESCAPE.get(ch)
                if decoded is None:
                    if ch == "u":
                        self._unicode_buf = ""
                        self._unicode_mode = True
                        continue
                    decoded = ch
                self._answer_chars.append(decoded)
                out.append(decoded)
                continue

            if getattr(self, "_unicode_mode", False):
                self._unicode_buf = getattr(self, "_unicode_buf", "") + ch
                if len(self._unicode_buf) == 4:
                    try:
                        out.append(chr(int(self._unicode_buf, 16)))
                        self._answer_chars.append(chr(int(self._unicode_buf, 16)))
                    except ValueError:
                        pass
                    self._unicode_mode = False
                continue

            if ch == "\\":
                self._escape = True
                continue
            if ch == '"':
                # ⚠️ 不能见到 " 就当结束 —— 正文里可能有引号。
                #    这里先记为「疑似结束」，等尾部回退缓冲确认。
                self._maybe_end = True
                self._tail = '"'
                continue
            if getattr(self, "_maybe_end", False):
                if ch in " \t\r\n":
                    self._tail = getattr(self, "_tail", "") + ch
                    continue
                if ch == "}":
                    self.state.finished = True
                    self.state.answer_done = True
                    self._maybe_end = False
                    self._tail = ""
                    continue
                if ch == ",":
                    # 见类文档最后一条：先按住，看逗号后面是不是别的字段
                    self._probe_head = getattr(self, "_tail", "")
                    self._probe = ","
                    self._probing = True
                    self._maybe_end = False
                    self._tail = ""
                    continue
                # 不是结束 —— 那是正文里的引号，回退补发
                self._maybe_end = False
                tail = getattr(self, "_tail", "")
                self._tail = ""
                for tch in tail:
                    self._answer_chars.append(tch)
                    out.append(tch)
                self._answer_chars.append(ch)
                out.append(ch)
                continue

            self._answer_chars.append(ch)
            out.append(ch)

        return out

    # ---- 收尾 -------------------------------------------------------
    def finish(self) -> tuple[str, str, str | None]:
        """流结束：严格解析完整 JSON，返回 (decision, answer, error)。"""
        if not self.state.decision:
            return "", "", "decision 字段缺失"
        if self.state.error:
            return self.state.decision, self.answer_text, self.state.error

        answer = self.answer_text
        decision = self.state.decision
        if decision == "REFUSED_NO_EVIDENCE":
            # 服务端忽略模型文案，改用固定话术
            return decision, REFUSAL_TEXT, None
        if not answer.strip():
            return decision, "", "ANSWERED 但正文为空"
        return decision, answer, None

    @property
    def answer_text(self) -> str:
        return "".join(self._answer_chars)


def extract_markers(answer: str, *, evidence_count: int) -> list[int]:
    """从答案正文派生引用编号（**唯一来源**）。"""
    return [int(m) for m in re.findall(r"\[(\d+)\]", answer or "")]


# 元陈述（§3.5.3 节点 9 的排除项，M3 的 K-1）：断言的不是**文档中的事实**，
# 而是「我查没查、资料里有没有」。方案给的原例就是「我查阅了资料」。
#
# ⚠️ M3 实测的误标：「资料中未找到针对黄色、橙色、红色预警各自具体后果的
#    进一步规定。」被当成结论句**标了灰** —— 那句话本身是在声明「没找到」，
#    灰标它属于误报。
# ⚠️ 判定方向与整条规则一致：**宁可漏，不可错** —— 认出来就排除（不标灰）。
_EVIDENCE_ABSENCE = re.compile(
    r"(?:"
    # 「资料中未找到 X」—— 材料词在前
    r"(资料|文档|知识库|原文|文中|材料)[^。？！]{0,16}?"
    r"(未找到|未提及|未包含|未涵盖|未涉及|未收录|未提供"
    r"|没有找到|没有提及|没有相关|不存在)"
    r"|"
    # 「未在文档中提及 X」—— 否定词在前（两种语序都真实存在）
    r"(未|没有)[^。？！]{0,8}?(资料|文档|知识库|原文|文中|材料)"
    r"[^。？！]{0,8}?(找到|提及|包含|涵盖|涉及|收录|提供)"
    r")"
)
_SELF_META = re.compile(r"^我(们)?(已)?(查阅|参考|阅读|检索|浏览|看了|查了)")


def is_conclusion_sentence(sentence: str) -> bool:
    """「结论句」定义 —— 生成与校验**共用同一套**。

    **结论句 = 断言了文档中事实的陈述句。** 三条**任一不满足即不是**：
      ① 是陈述句（疑问句、祈使/建议句不算）
      ② 断言了文档中的事实（对用户的建议、澄清反问、复述、元陈述不算）
      ③ 有实质内容（纯过渡句与过短句不算）

    ⚠️ **判定必须保守：拿不准的一律不算结论句，不标灰。宁可漏，不可错。**
       四处引用「结论句」若各自理解，就会出现「模型认为它不是结论句、
       校验器认为它是」，把一句正确的话标成无依据 —— **这比漏标伤害更大**。
    """
    text = (sentence or "").strip()
    if not text:
        return False
    if text.endswith("？") or text.endswith("?"):
        return False
    if text.startswith(("请", "建议", "应当", "需", "可以", "如需")):
        return False
    # 元陈述：对「资料里有没有」的声明，不是对事实的断言（§3.5.3 节点 9）
    if _EVIDENCE_ABSENCE.search(text) or _SELF_META.match(text):
        return False
    if any(text.startswith(t) for t in _TRANSITION) and len(text) < 12:
        return False
    if len(text) < 8:
        return False
    return True


def _stream_writer():
    """取 LangGraph 的 custom stream writer（不在图内运行时返回 None）。"""
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:  # noqa: BLE001
        return None


async def _stream_generate(prompt: str, writer, timeout: float) -> tuple[str, str, str | None]:
    """流式生成：边收边解析，把**解码后的纯文本**通过 writer 下发。

    返回 (decision, answer, error)。
    降级规格（§3.5.3 节点 9）：① 停止下发 token ② 发 `error`
    （`code="upstream_error"`）③ **不发 refused** ④ **保留已流出的 token** ⑤ 不自动重试。
    """
    parser = StreamJsonParser()
    first = True

    try:
        async for piece in llm.stream_raw(
            [{"role": "user", "content": prompt}], timeout=timeout
        ):
            if first:
                first = False
                if not parser.precheck_prefix(piece):
                    # 首字符校验失败：去掉 BOM/空白/围栏后仍不以 { 开头
                    writer({"type": "error", "code": "upstream_error",
                            "message": "模型输出格式异常"})
                    return "", "", "首字符校验失败"
            for text in parser.feed(piece):
                # ⚠️ REFUSED 路径**不发模型文案**（§3.5.3 节点 9：避免模型在拒答时
                #    夹带解释或猜测）。decision 在 answer 之前就解析出来了，
                #    所以这里判得住；否则用户会先看到模型原话、
                #    下面再叠一个服务端拒答框，刷新后又变了个样。
                if text and parser.state.decision != "REFUSED_NO_EVIDENCE":
                    writer({"type": "token", "text": text})
            if parser.state.finished:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("流式生成中断：%s", e, extra={"event": "generate.stream_failed",
                                                "node": "generate"})
        # ⚠️ 不发 refused —— 这是上游格式错误，不是拒答；保留已流出的 token
        writer({"type": "error", "code": "upstream_error",
                "message": "生成中断，请重试"})
        return "", parser.answer_text, str(e)

    decision, answer, error = parser.finish()
    if error:
        writer({"type": "error", "code": "upstream_error", "message": error})
    return decision, answer, error


async def generate_node(state) -> dict:
    """生成节点。有 stream writer 时走流式（边收边发 token），否则一次性返回。"""
    started = time.perf_counter()
    query = state.get("resolved_query") or state.get("query", "")
    context = state.get("context") or ""
    evidence = state.get("evidence") or []

    if not evidence:
        return {
            "decision": "REFUSED_NO_EVIDENCE",
            "answer": REFUSAL_TEXT,
            "refused": True,
            "refusal_reason": "no_candidate",
            "citations": [],
            "verify_report": _empty_report(),
            "trace": [_trace(started, 0, None)],
        }

    prompt = render("generate",
        # 顺序即 §3.8.5：[系统][摘要][messages[compressed_count:]][检索上下文][本轮问题]
        summary=(state.get("summary") or "").strip() or "（无）",
        history=_format_history(state.get("history") or []),
        context=context,
        query=query,
    )

    # ---- B 路的最后一道闸（§3.8.2 / §3.8.3 溢出兜底）------------------
    # 历史预算是**成本目标**不是硬限；这里才是真硬限。
    # ⚠️ 真撞上时**不能裁检索上下文**：它已被 build_context 限死在 8,000 token，
    #    对着 100 万 token 的窗口裁它救不了任何东西 —— 唯一可能无界增长的只有历史。
    #    所以这里的兜底是「先把历史压掉再试」；压完还超就按契约报
    #    context_length_exceeded（**不是**拒答）。
    if count_tokens(prompt) > hard_prompt_limit():
        session_id = state.get("session_id") or ""
        slice_ = None
        if session_id:
            try:
                slice_ = await context_service.force_compact(session_id)
            except Exception as e:  # noqa: BLE001
                # 强制压缩失败也不能把这一轮变成 500：拿原 prompt 继续，
                # 后面若仍超硬限会走 context_length_exceeded（一个明确的错误码）
                logger.warning("强制压缩失败：%s", e,
                               extra={"event": "compaction.force_failed", "node": "generate"})
                slice_ = None
        if slice_ is not None:
            prompt = render("generate",
                summary=(slice_.summary or "").strip() or "（无）",
                history=_format_history(slice_.messages),
                context=context,
                query=query,
            )
        if count_tokens(prompt) > hard_prompt_limit():
            writer = _stream_writer()
            if writer is not None:
                writer({"type": "error", "code": "context_length_exceeded",
                        "message": "上下文超出模型窗口，请新开会话"})
            return _degraded(started, "")

    timeout = float(cfg("timeouts.generate_ttft", 60))
    writer = _stream_writer()

    if writer is not None:
        decision, answer, error = await _stream_generate(prompt, writer, timeout)
        if error:
            # ⚠️ 上游格式错误/中断 **不是拒答**（§3.5.3 节点 9「降级」五条第 ③ 条）：
            #    不能换成拒答文案、更不能置 `refused` —— 那会把「模型输出坏了」
            #    记成「知识库没有依据」，用户与评测都会被误导。
            #    已流出的正文保留；但要留痕（degraded），否则评测护栏看不出这轮是坏的。
            return _degraded(started, answer or "")
    else:
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": prompt}], timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            logger.warning("生成失败，降级为拒答",
                           extra={"event": "generate.failed", "node": "generate"})
            return _refuse_insufficient(started, 0)
        decision = str(data.get("decision") or "").strip()
        answer = str(data.get("answer") or "")

    if decision == "REFUSED_NO_EVIDENCE":
        return {
            "decision": "REFUSED_NO_EVIDENCE",
            "answer": REFUSAL_TEXT,          # 忽略模型文案
            "refused": True,
            "refusal_reason": "insufficient_evidence",
            "citations": [],
            "verify_report": _empty_report(),
            "trace": [_trace(started, 0, None)],
        }

    # 服务端确定性校验：ANSWERED 正文至少含一个合法标记
    markers = extract_markers(answer, evidence_count=len(evidence))
    valid = [m for m in markers if 1 <= m <= len(evidence)]
    if not valid:
        logger.warning("ANSWERED 但正文无合法标记，降级为拒答",
                       extra={"event": "generate.invalid_markers", "node": "generate"})
        return _refuse_insufficient(started, len(answer))

    return {
        "decision": "ANSWERED",
        "answer": answer,
        "refused": False,
        "refusal_reason": "",
        "trace": [_trace(started, len(answer), None)],
    }


def _trace(started: float, recalled: int, degraded: str | None):
    from app.graph.state import NodeTrace
    return NodeTrace(node="generate",
                     ms=int((time.perf_counter() - started) * 1000),
                     recalled=recalled, degraded=degraded)


def _degraded(started: float, answer: str) -> dict:
    """降级态（区别于拒答）：保留已流出的正文，不置 `refused`。

    `degraded` 取 `unavailable` —— 词表见 §4.1（timeout/oom/model_load_failed/
    index_invalid/unavailable）。这里没有「上游格式错误」这一档，取语义最近的
    「上游不可用」，不新造取值（词表是契约）。
    """
    return {
        "decision": "",
        "answer": answer,
        "refused": False,
        "refusal_reason": "",
        "trace": [_trace(started, len(answer), "unavailable")],
    }


def _refuse_insufficient(started: float | None = None, recalled: int = 0) -> dict:
    out = {
        "decision": "REFUSED_NO_EVIDENCE",
        "answer": REFUSAL_TEXT,
        "refused": True,
        "refusal_reason": "insufficient_evidence",
        "citations": [],
        "verify_report": _empty_report(),
    }
    if started is not None:
        out["trace"] = [_trace(started, recalled, None)]
    return out


def _format_history(history) -> str:
    """拼历史。

    ⚠️ **不再截断**（原实现取 `history[-6:]`）：进 state 的历史已经是
       `context_service` 按 token 预算压过的 `messages[compressed_count:]`。
       在这里再按条数切一刀，会出现「数的是 20 条、发的是 6 条」——
       水位线算的历史与实际下发的历史不是同一份，压缩就永远不收敛。
    """
    if not history:
        return "（无）"
    lines = []
    for msg in history:
        role = "用户" if getattr(msg, "role", "") == "user" else "助手"
        # ⚠️ 拼 history 时剥掉助手消息里的 [n] 标记（落库保留原文）
        content = re.sub(r"\[\d+\]", "", getattr(msg, "content", "") or "")
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _empty_report():
    from app.schemas.chat import VerifyReport
    return VerifyReport()
