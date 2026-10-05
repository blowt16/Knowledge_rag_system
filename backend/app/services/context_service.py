"""上下文预算与增量滚动压缩（§3.8，M3-1）。

完整历史 = `compressed_summary` + `messages[compressed_count:]`（§3.8.3）。

⚠️ **摘要是历史的一部分、计入 16K 预算**。两种读法后果相反：摘要不计入 →
   摘要越长、可压的原文越少，触发线却不动，压缩永远不收敛。本实现按方案
   钉死的口径：**计入**；`summary_max_tokens=800` 是摘要**自身**的上限。

⚠️ **A / B 回收顺序**（§3.8.2，两条顺序不能混）：

| 触发 | 顺序 | 动不动检索上下文 |
|---|---|---|
| **A** 历史自身超 16K 目标 | 压缩历史（本模块） | ❌ **绝不动** |
| **B** 整个 prompt 真溢出硬上限 | ① 同 A ② 再裁检索上下文 ③ 报错 | ✅ 只有这条会动 |

   A 不碰检索上下文的理由：检索到的是本轮**回答依据**，历史只是理解指代的
   背景。把依据裁掉、留下背景 —— 方向反了。

   本模块**只做 A**。B 的入口在 `generate`（真撞上硬上限时它才出手），
   且 B 的 ② 在本项目里没有意义：检索上下文已被 `build_context` 限死在
   8,000 token，对着 100 万 token 的窗口，裁掉它救不了任何东西 ——
   唯一可能无界增长的只有历史。

⚠️ **压缩失败绝不能让用户看到 500**（§3.8.3 硬约束）：摘要模型超时/报错 →
   退回**未压缩**的历史继续作答，记 `degradation_events(node="compaction")`。
   压缩是优化手段，不是回答的必经之路。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app import db
from app.core import llm
from app.core.config import cfg
from app.core.prompts import render
from app.graph.state import Message, NodeTrace

logger = logging.getLogger(__name__)

# token 计数用 dashscope 自带分词器（离线可用、零新增依赖）
_tokenizer = None


def count_tokens(text: str) -> int:
    """token 计数的**唯一入口** —— build_context 也用这份，避免两处口径不一致。

    ⚠️ 定位是「近似」（施工计划 D-3）：DeepSeek 分词器不可离线获取，
       中文 BPE 量级相近，且水位线本身是成本目标而非硬限。
    """
    global _tokenizer
    if _tokenizer is None:
        try:
            from dashscope.tokenizers import get_tokenizer
            _tokenizer = get_tokenizer(cfg("llm.tokenizer_model", "qwen3-max"))
        except Exception:  # noqa: BLE001 —— 拿不到分词器时退到保守估算
            _tokenizer = False
    if _tokenizer:
        try:
            return len(_tokenizer.encode(text))
        except Exception:  # noqa: BLE001
            pass
    # 保守估算：中文约 0.7 token/汉字（文档实测 1.72 字符/token）
    return int(len(text) * 0.7)


@dataclass
class ContextSlice:
    """本轮要用的历史切面。"""
    summary: str = ""
    messages: list[Message] = field(default_factory=list)
    # 本次并进摘要的消息条数（0 = 没触发压缩）
    compacted: int = 0
    # 压缩失败的降级类型（timeout / unavailable），None = 没降级
    degraded: str | None = None
    ms: int = 0

    def trace(self) -> NodeTrace:
        """压缩也要留痕（写进 qa_logs.node_timings）—— 静默兜底等于没有兜底。"""
        return NodeTrace(node="compaction", ms=self.ms,
                         recalled=self.compacted, degraded=self.degraded)


# ---- 水位线（§3.8.2 / §3.8.3）------------------------------------------

def _history_budget() -> int:
    return int(cfg("context.history_token_budget", 16000))


def _high_water() -> int:
    """高水位 H：历史超过它就压缩。写成比例是提示「按成本目标定的，不是硬限」。"""
    return int(_history_budget() * float(cfg("context.high_water_ratio", 0.8)))


def _low_water() -> int:
    """低水位 L：压到这里就停 —— 没有它就会**每轮都调一次摘要模型**。"""
    return int(_history_budget() * float(cfg("context.low_water_ratio", 0.5)))


def _summary_cap() -> int:
    return int(cfg("context.summary_max_tokens", 800))


def _reserve() -> int:
    """保留区条数：最近这么多条**原文永不动**（压缩只吃更老的那些）。"""
    return int(cfg("context.recent_messages_limit", 10))


def _min_merge() -> int:
    return int(cfg("context.compression_min_messages", 6))


def hard_prompt_limit() -> int:
    """整个 prompt 的可用上限（§3.8.2）：窗口 − 最大输出 − 安全余量 500。"""
    window = int(cfg("llm.context_window", 1048576))
    out = int(cfg("llm.max_output_tokens_runtime", 8192))
    return window - out - int(cfg("context.safety_margin", 500))


def history_tokens(summary: str, messages: list[Message]) -> int:
    """★ 触发判据用的就是它：**摘要 + 未压缩消息**（§3.8.3 第 3 条）。"""
    return count_tokens(summary or "") + sum(count_tokens(m.content or "") for m in messages)


# ---- 读 ----------------------------------------------------------------

async def load_context(session_id: str) -> ContextSlice:
    """取本轮要用的历史；超水位就在**请求路径内同步压缩**。

    ⚠️ 读到的天然**不含本轮** —— 本轮消息在回答生成完成后才写库（§3.8.5）。
    """
    started = time.perf_counter()
    summary, compressed_count, messages = await _read(session_id)

    if not messages and not summary:
        return ContextSlice(ms=_elapsed(started))

    if history_tokens(summary, messages) <= _high_water():
        return ContextSlice(summary=summary, messages=messages, ms=_elapsed(started))

    try:
        new_summary, merged = await _compact(summary, messages)
    except Exception as e:  # noqa: BLE001 —— 摘要模型坏了不能拦住这一轮回答
        kind = "timeout" if isinstance(e, llm.LLMTimeout) else "unavailable"
        logger.warning("滚动压缩失败，退回未压缩历史：%s", e,
                       extra={"event": "compaction.failed", "node": "compaction"})
        await _record_degradation(session_id, kind, str(e))
        return ContextSlice(summary=summary, messages=messages,
                            degraded=kind, ms=_elapsed(started))

    if merged:
        # ⚠️ 两列**同一事务、同一条语句**写 —— 只写一个会让「摘要 + 尾部」
        #    拼不起来（摘要覆盖了 A 段，compressed_count 却还停在原处）
        await _write_back(session_id, new_summary, compressed_count + merged)
        logger.info("滚动压缩完成", extra={
            "event": "compaction.done", "node": "compaction",
            "merged": merged, "before": compressed_count + merged,
        })

    return ContextSlice(summary=new_summary, messages=messages[merged:],
                        compacted=merged, ms=_elapsed(started))


async def force_compact(session_id: str) -> ContextSlice:
    """强制压缩一次（§3.8.3 的溢出兜底 / B 路第一步）。

    与 `load_context` 的区别：**不看水位线**，能压多少压多少（保留区仍不动）。
    """
    summary, compressed_count, messages = await _read(session_id)
    new_summary, merged = await _compact(summary, messages, force=True)
    if merged:
        await _write_back(session_id, new_summary, compressed_count + merged)
    return ContextSlice(summary=new_summary, messages=messages[merged:], compacted=merged)


# ---- 压缩 --------------------------------------------------------------

async def _compact(summary: str, messages: list[Message],
                   *, force: bool = False) -> tuple[str, int]:
    """把压缩区里**最老**的若干条并入摘要，返回 (新摘要, 并入条数)。

    - 压缩区 = `messages[: len - 保留区]`：保留区（最近 10 条）永不入摘要
    - **单次至少 6 条**：否则每轮都调一次摘要模型（滞回的意义就在这里）
    - 目标：剩余历史 ≤ L。摘要长度按**上限**估算（保守），
      所以**一次调用**就能定下来，不反复试
    """
    reserve = _reserve()
    compressible = max(0, len(messages) - reserve)
    if compressible <= 0:
        return summary, 0

    costs = [count_tokens(m.content or "") for m in messages]
    if force:
        merged = compressible
    else:
        # 摘要最多占 _summary_cap()，留给「未压缩原文」的就是 L 减掉它
        target = max(0, _low_water() - _summary_cap())
        remaining = sum(costs)
        merged = 0
        while merged < compressible and (merged < _min_merge() or remaining > target):
            remaining -= costs[merged]
            merged += 1

    if merged <= 0:
        return summary, 0

    new_summary = await _summarize(summary, messages[:merged])
    if not new_summary.strip():
        # ⚠️ **空摘要绝不能回写**（2026-10-05 评审抓出）：写进去 = 这 N 条消息
        #    永久消失 —— conversations 只有一列摘要，旧摘要会被就地销毁，
        #    而没有任何东西代表那批消息。用户侧表现是「助手突然忘了前面聊过什么」，
        #    且不可恢复。`llm.complete` 对空 content **不抛异常**，
        #    所以不显式检查就会走「成功」分支（连降级事件都不记）。
        #    按压缩失败处理：不回写、不推进 compressed_count、由调用方记降级。
        raise llm.LLMError("摘要模型返回空内容")
    return new_summary, merged


async def _summarize(previous: str, merged: list[Message]) -> str:
    prompt = render(
        "summarize",
        previous=(previous or "（无）").strip(),
        messages=_format(merged),
    )
    text = await llm.complete(
        [{"role": "user", "content": prompt}],
        timeout=float(cfg("timeouts.compaction", 20)),
        # ⚠️ 摘要自身有上限，否则它会一轮比一轮胖，把窗口撑爆
        max_tokens=_summary_cap(),
    )
    return (text or "").strip()


def _format(messages: list[Message]) -> str:
    lines = []
    for msg in messages:
        role = "用户" if msg.role == "user" else "助手"
        lines.append(f"{role}：{_strip_markers(msg.content)}")
    return "\n".join(lines)


# ---- 存取 --------------------------------------------------------------

async def _read(session_id: str) -> tuple[str, int, list[Message]]:
    async with db.tx() as conn:
        row = await conn.fetchrow(
            "SELECT compressed_summary, compressed_count FROM conversations WHERE id = $1",
            session_id,
        )
        if row is None:
            return "", 0, []
        rows = await conn.fetch(
            """SELECT role, content FROM messages
                WHERE conversation_id = $1
                ORDER BY created_at ASC, id ASC
                OFFSET $2""",
            session_id, int(row["compressed_count"] or 0),
        )
    messages = [Message(role=r["role"], content=r["content"] or "") for r in rows]
    return (row["compressed_summary"] or ""), int(row["compressed_count"] or 0), messages


async def _write_back(session_id: str, summary: str, compressed_count: int) -> None:
    async with db.tx() as conn:
        await conn.execute(
            """UPDATE conversations
                  SET compressed_summary = $2, compressed_count = $3
                WHERE id = $1""",
            session_id, summary, compressed_count,
        )


async def _record_degradation(session_id: str, kind: str, detail: str) -> None:
    """记降级事件（与 `degradation_events.kind` 同词表：timeout / unavailable…）。"""
    try:
        async with db.tx() as conn:
            await conn.execute(
                """INSERT INTO degradation_events (id, session_id, node, kind, detail)
                   VALUES (md5(random()::text || clock_timestamp()::text),
                           $1, 'compaction', $2, $3)""",
                session_id, kind, detail[:500],
            )
    except Exception:  # noqa: BLE001 —— 记降级失败不该影响主链路
        logger.warning("写压缩降级事件失败", extra={"event": "degradation.write_failed"})


# ---- 工具 --------------------------------------------------------------

_MARKER_RE_SOURCE = r"\[\d+\]"


def _strip_markers(text: str) -> str:
    """拼历史时剥掉 `[n]`（§3.8.4 防线①）—— 模型会模仿旧编号。"""
    import re
    return re.sub(_MARKER_RE_SOURCE, "", text or "")


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
