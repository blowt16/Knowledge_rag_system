"""节点 2：route —— 三分类路由（规则 + LLM 两层）（§3.5.3 节点 2）。

**规则层两条收严原则**：
1. **规则层不判 `clarify`** —— clarify 的语义就是「意图模糊」，
   而规则恰恰判断不了模糊。规则层只负责**明确的**那两类。
2. **规则层只做减法，不做加法判断** —— 只有"确定是闲聊"才拦，
   **不做"短 → 闲聊"的推断**。

⚠️ **已删除原设计中的「短句判定」**：按字面实现，「挂科了怎么办」「怎么申请」
   「还有吗」这类短问题会被判成闲聊，而 `chat` 分支明确**不检索、不引用** ——
   **直接答非所问，且规则层没有 LLM 复核机会**。这是全文档最危险的一条规则。

⚠️ **冲突优先级：`knowledge` > `chat`**。

⚠️ **`last_route` 的取值域必须收窄 —— 只有 `knowledge` 参与「保持上一轮」**：

| 上一轮是 | 本轮分类不明确时 | 为什么 |
|---|---|---|
| `knowledge` | 保持 `knowledge` | 多轮追问同一主题，这正是「粘性」要解决的场景 |
| `chat` | **不保持** | 否则「你好」→「那这个要多久啊」被粘成 chat，**不检索不引用，答非所问** |
| `clarify` | **不保持** | 否则用户回答澄清后的新问题会被**再次拽回 clarify**，反复反问 |

⚠️ 兜底时 `route_source` 记 **`llm`**（不是 `rule`）—— 它确实经过了 LLM 调用，
   只是没拿到可用结果。不写清的话「规则命中率」的口径会被污染。
"""

from __future__ import annotations

import logging
import re
import time

from app.core import llm
from app.core.prompts import render
from app.core.config import cfg
from app.graph.state import RAGState
from app.graph.nodes.resolve import is_meaningless

logger = logging.getLogger(__name__)



def _policy_nouns() -> list[str]:
    return cfg("rules.policy_nouns", []) or []


def _question_words() -> list[str]:
    return cfg("rules.question_words", []) or []


def rule_classify(query: str) -> str | None:
    """规则层。只吃**明确的**意图，零成本跳过 LLM 调用。

    返回 None 表示规则层未命中，交给 LLM 层。
    """
    text = (query or "").strip()
    if not text:
        return "chat"

    # knowledge：文号正则 —— 命中即 knowledge（保文号精度，误判代价最大的一类）
    pattern = cfg("rules.doc_number_pattern", "")
    if pattern and re.search(pattern, text):
        return "knowledge"

    # knowledge：制度名词 + 疑问词
    if any(n in text for n in _policy_nouns()) and \
       any(q in text for q in _question_words()):
        return "knowledge"

    # knowledge 优先于 chat（宁可多检索，不可漏答）
    if any(n in text for n in _policy_nouns()):
        return "knowledge"

    # chat：仅精确无信息量词表（判据同 resolve 的闸门）
    if is_meaningless(text):
        return "chat"

    # 规则层不判 clarify
    return None


def _should_clarify(state: RAGState) -> bool:
    """澄清触发**仅两种**（§3.5.3 节点 2 / 节点 4）。

    **该直接回答时反问用户，是体验最差的失败模式 —— 宁可多答，不可多问。**
    """
    if state.get("resolve_unresolved"):
        return True

    resolved = (state.get("resolved_query") or state.get("query") or "").strip()
    min_len = int(cfg("clarify.min_length", 8))
    if len(resolved) >= min_len:
        return False
    # 「无法定位主题」：规则层未命中任何制度名词 / 文号 / 实体（与规则层共用词表）
    if any(n in resolved for n in _policy_nouns()):
        return False
    pattern = cfg("rules.doc_number_pattern", "")
    if pattern and re.search(pattern, resolved):
        return False
    return True


async def route_node(state: RAGState) -> dict:
    started = time.perf_counter()
    query = state.get("resolved_query") or state.get("query", "")

    # ⚠️ 闸门必须先于澄清判据：「你好」这类**没有实际诉求**的消息是 `chat`，
    #    而它也「短且无法定位主题」，会被澄清规则误吞。
    #    2026-10-05 实测踩过：问「你好」得到 route=clarify 且 facets 为空 ——
    #    用户打招呼却被反问「你想问的是哪一项？」，正是文档说的
    #    「该直接回答时反问用户，是体验最差的失败模式」。
    if is_meaningless(query):
        return _result("chat", "rule", started)

    # 满足 clarify 触发条件时**优先归 clarify，不受上轮分类约束** ——
    # 否则「上一轮 knowledge + 本轮『那个呢？』」会被拉回 knowledge 直接检索，
    # 澄清分支永远触发不了。
    if _should_clarify(state):
        return _result("clarify", "rule", started)

    route = rule_classify(query)
    source = "rule"
    degraded: str | None = None

    if route is None:
        source = "llm"
        route, degraded = await _llm_classify(query, state.get("last_route", ""))

    return _result(route, source, started, degraded)


async def _llm_classify(query: str, last_route: str) -> tuple[str, str | None]:
    """返回 (路由, 降级类型)。**兜底必须留痕** —— 见 `_result` 的说明。"""
    # ⚠️ 只有 knowledge 参与「保持上一轮」；chat / clarify 视为「无上轮分类」
    sticky = last_route if last_route == "knowledge" else "（无）"
    prompt = render("route", last_route=sticky, query=query)
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": prompt}],
            timeout=float(cfg("timeouts.route", 5)),
            max_tokens=64,
        )
        value = str(data.get("route") or "").strip().lower()
        if value in ("chat", "clarify", "knowledge"):
            return value, None
        logger.warning("路由 LLM 返回非法取值 %r，兜底为 knowledge", value,
                       extra={"event": "route.illegal_value", "node": "route"})
    except llm.LLMTimeout:
        logger.warning("路由 LLM 超时，兜底为 knowledge",
                       extra={"event": "route.timeout", "node": "route"})
        return "knowledge", "timeout"
    except Exception:  # noqa: BLE001
        logger.warning("路由 LLM 失败，兜底为 knowledge",
                       extra={"event": "route.fallback", "node": "route"})
        return "knowledge", "unavailable"
    # 解析失败 / 非法取值 → knowledge（宁可多检索，不可漏答）
    return "knowledge", "unavailable"


def _result(route: str, source: str, started: float,
            degraded: str | None = None) -> dict:
    """⚠️ `degraded` 不是装饰：`app.cli eval-multiturn` 的护栏靠它判断
       「这一轮的指标还反不反映设计行为」，有降级就拒绝写评测文件。
       兜底了却不留痕 = 护栏拦不住，指标照样进了验收文档（评审 I2）。"""
    from app.graph.state import NodeTrace
    return {
        "route": route,
        "route_source": source,   # rule | llm —— 规则命中率的数据源
        "trace": [NodeTrace(node="route", ms=int((time.perf_counter() - started) * 1000),
                            recalled=0, degraded=degraded)],
    }
