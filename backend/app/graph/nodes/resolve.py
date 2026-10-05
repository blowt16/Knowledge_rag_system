"""节点 1：resolve —— 指代消解与语义补全（§3.5.3 节点 1）。

跳过原因**必须分类记录**（`resolve_skipped_reason`），不能只用一个布尔：

| 取值 | 含义 | 与「消解后检索命中率」的关系 |
|---|---|---|
| `gate` | 命中无信息量词表（闸门） | **应排除出对照样本** |
| `no_history` | 无历史可消解 | 同上 |
| `no_feature` | 无代词且无省略特征 | 同上 |
| `timeout` | LLM 超时被动跳过 | **必须排除** —— 失败样本会污染对照指标 |
| `none` | 未跳过 | 对照样本 |

原设计只有一个 `skipped_resolve: bool`，两种读法都成立：
只在超时置位 → 「跳过率」统计不出来；所有跳过都置位 → 对照指标被超时样本污染。
**两种跳过的性质完全不同**（主动闸门 vs 被动失败），必须分开。

⚠️ 判据与节点 2 完全一致：**「整条消息没有实际诉求」**，
   不是「包含一个无信息量词」。「你好，那它的条件呢？」有实际诉求，
   命中词表也不该跳过 —— **拿不准时不跳过**（knowledge > chat）。

⚠️ 「继续」**刻意不入**无信息量词表：它在多轮里是有效诉求。
"""

from __future__ import annotations

import logging
import re
import time

import jieba

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.graph.state import RAGState

logger = logging.getLogger(__name__)



def _gate_words() -> list[str]:
    return cfg("rules.gate_words", []) or []


def _pronoun_words() -> list[str]:
    return cfg("rules.pronoun_words", []) or []


def _policy_nouns() -> list[str]:
    return cfg("rules.policy_nouns", []) or []


def _question_words() -> list[str]:
    return cfg("rules.question_words", []) or []


def is_meaningless(query: str) -> bool:
    """闸门判据：**整条消息没有实际诉求**。

    ⚠️ 命中词表只是**必要条件**，还要满足「去掉这些词后消息不剩实质内容」。
       按「命中即触发」实现的话，「你好，那它的条件呢？」会被当成闲聊 ——
       **有实际诉求的问题被跳过消解、判 chat、不检索不引用，直接答非所问。**
    """
    text = (query or "").strip()
    if not text:
        return True

    lowered = text.lower()
    for word in _gate_words():
        if not word:
            continue
        if word.lower() in lowered:
            residue = re.sub(re.escape(word), "", text, flags=re.IGNORECASE).strip()
            residue = re.sub(r"[\s，。！？、,.!?~～]+", "", residue)
            if not residue:
                return True

    # 冲突优先级 knowledge > chat：含制度名词/文号/疑问词时一律不跳过
    if any(n in text for n in _policy_nouns()):
        return False
    if re.search(cfg("rules.doc_number_pattern", ""), text):
        return False
    return False


def has_ellipsis_feature(query: str) -> bool:
    """省略特征三条（§3.5.3 节点 1）。

    ⚠️ 中文多轮里**省略比指代更常见，而且省略句往往没有代词**：
       「需要什么条件？」「那 2025 级的呢？」「多久？」都不含代词。
       **只判代词会漏掉这三种**，而「多久？」原样送检索几乎不可能命中。

       **已删除「长度 < 阈值」触发条件** —— 原设计下有历史时任何短消息
       （「你好」「谢谢」「嗯」「继续」）都会触发消解，**而这恰好是它想省掉的调用**。
    """
    text = (query or "").strip()
    if not text:
        return False

    words = [w for w in jieba.lcut(text) if w.strip()]

    # ① 含疑问词且无实义名词
    has_question = any(w in _question_words() for w in words) or \
        any(q in text for q in _question_words())
    has_policy_noun = any(n in text for n in _policy_nouns())
    if has_question and not has_policy_noun:
        return True

    # ② 以「呢」结尾（「那 2025 级的呢？」）
    if text.endswith("呢"):
        return True

    # ③ 长度短 **且不含制度名词**
    #    「缓考」「转专业」这类制度名词本身就是自足查询，不该被补全
    if len(text) < 8 and not has_policy_noun:
        return True

    return False


def has_pronoun(query: str) -> bool:
    """⚠️ 必须 jieba 分词后按词匹配，不能子串匹配 ——
    否则「**其**他」会误命中「他」。"""
    words = {w for w in jieba.lcut(query or "") if w.strip()}
    return any(p in words for p in _pronoun_words())


def should_resolve(state: RAGState) -> tuple[bool, str]:
    """返回 (是否调用 LLM, 跳过原因)。"""
    query = state.get("query", "")
    if is_meaningless(query):
        return False, "gate"
    history = state.get("history") or []
    if not history:
        return False, "no_history"
    if not (has_pronoun(query) or has_ellipsis_feature(query)):
        return False, "no_feature"
    return True, "none"


def _format_history(history) -> str:
    lines = []
    for msg in history[-8:]:
        who = "用户" if getattr(msg, "role", "") == "user" else "助手"
        lines.append(f"{who}：{getattr(msg, 'content', '')}")
    return "\n".join(lines)


async def resolve_node(state: RAGState) -> dict:
    started = time.perf_counter()
    query = state.get("query", "")

    need, reason = should_resolve(state)
    if not need:
        return {
            "resolved_query": query,
            "resolve_skipped_reason": reason,
            "trace": [_trace(started)],
        }

    prompt = render("resolve", history=_format_history(state.get("history") or []),
                            query=query)
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": prompt}],
            timeout=float(cfg("timeouts.resolve", 5)),
            max_tokens=512,
        )
        resolved = str(data.get("resolved") or "").strip()
        # 输出做格式校验（长度上限），拒绝异常内容
        if not resolved or len(resolved) > 1000:
            resolved = query
    except llm.LLMTimeout:
        # LLM 超时 → 透传原 query，并记 timeout（失败样本，必须与 gate 分开）
        logger.warning("消解超时，透传原问题", extra={"event": "resolve.timeout",
                                                "node": "resolve"})
        return {
            "resolved_query": query,
            "resolve_skipped_reason": "timeout",
            "trace": [_trace(started, degraded="timeout")],
        }
    except Exception:  # noqa: BLE001 —— 解析失败也透传，不让请求失败
        logger.warning("消解失败，透传原问题", extra={"event": "resolve.failed",
                                                "node": "resolve"})
        return {
            "resolved_query": query,
            "resolve_skipped_reason": "timeout",
            "trace": [_trace(started, degraded="model_load_failed")],
        }

    return {
        "resolved_query": resolved,
        "resolve_skipped_reason": "none",
        # 指代无法消解 —— clarify 的触发依据之一（不在这里重复判断）
        "resolve_unresolved": (not resolved) or resolved == query and has_pronoun(query),
        "trace": [_trace(started)],
    }


def _trace(started: float, degraded: str | None = None):
    from app.graph.state import NodeTrace
    return NodeTrace(node="resolve", ms=int((time.perf_counter() - started) * 1000),
                     recalled=0, degraded=degraded)
