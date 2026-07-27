"""意图分类器 — 独立轻量 LLM 完成意图前置判定。"""
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)

# 单例
_classifier: Optional["IntentClassifier"] = None
_lock = threading.Lock()


@dataclass
class IntentResult:
    """意图分类结构化结果。"""
    intent: str = "NEED_RAG"           # NEED_RAG | NO_RAG
    confidence: float = 0.5
    reason: str = ""
    search_query: str = ""
    action_directive: str = ""

    def is_rag_needed(self) -> bool:
        return self.intent == "NEED_RAG"


class IntentClassifier:
    """意图分类器：调用独立轻量 LLM，前置判断 query 是否需要检索。

    使用 DeepSeek 小版本模型，temperature=0，同步调用。
    """

    def __init__(self):
        self._model = None
        self._cache: dict[str, tuple[float, "IntentResult"]] = {}
        self._prompt_loader = PromptLoader()
        self._cache_max_size = int(get_config("intent_cache_max_size", 500))
        self._model_lock = threading.Lock()

    # ── 公开接口 ──────────────────────────────────────────

    def classify(
        self, query: str, session_id: str,
        history: list | None = None,
    ) -> IntentResult:
        """同步分类，返回结构化结果。异常时降级返回 NEED_RAG。"""
        t_start = time.time()

        # 1. 总开关关闭 → 直接返回 NEED_RAG（跳过识别，走原有流程）
        if not get_config("intent_enabled", True):
            return IntentResult(
                intent="NEED_RAG", confidence=1.0,
                reason="意图识别已关闭", action_directive="",
            )

        # 2. 检查缓存
        cache_key = self._cache_key(query, session_id)
        if cache_key in self._cache:
            cached_at, result = self._cache[cache_key]
            if time.time() - cached_at < int(get_config("intent_cache_ttl", 300)):
                logger.debug(f"【意图识别】缓存命中: intent={result.intent}, "
                             f"cache_size={len(self._cache)}")
                return result
            else:
                # 过期条目清理
                del self._cache[cache_key]
                logger.debug(f"【意图识别】缓存条目过期，已清理")

        # 3. 调用轻量 LLM
        try:
            result = self._do_classify(query, history)
            if result is None:
                result = self._fallback_result("LLM 返回空")
        except Exception as e:
            logger.warning(f"【意图识别】分类异常: {e}")
            result = self._fallback_result(str(e))

        # 4. 低置信度 → 兜底 NEED_RAG
        threshold = float(get_config("intent_confidence_threshold", 0.7))
        if result.confidence < threshold:
            logger.info(
                f"【意图识别】置信度低于阈值 ({result.confidence:.2f} < {threshold})，"
                f"降级 NEED_RAG"
            )
            result = self._fallback_result(
                f"低置信度({result.confidence:.2f})",
            )

        # 5. 缓存（超过上限时淘汰最旧的一半条目）
        if len(self._cache) >= self._cache_max_size:
            stale_count = self._cache_max_size // 2
            sorted_entries = sorted(self._cache.items(), key=lambda x: x[1][0])
            for old_key, _ in sorted_entries[:stale_count]:
                del self._cache[old_key]
            logger.info(
                f"【意图识别】缓存淘汰: removed={stale_count}, "
                f"remaining={len(self._cache)}"
            )
        self._cache[cache_key] = (time.time(), result)

        elapsed = time.time() - t_start
        logger.info(
            f"【意图识别】session={session_id[:8]}..., intent={result.intent}, "
            f"confidence={result.confidence:.2f}, reason={result.reason}, "
            f"latency={elapsed:.2f}s"
        )
        return result

    # ── 内部方法 ──────────────────────────────────────────

    def _get_model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from app.utils.factory import create_intent_model
                    self._model = create_intent_model()
                    logger.info("【意图识别】模型初始化完成")
        return self._model

    def _do_classify(self, query: str, history: list | None) -> Optional[IntentResult]:
        """执行 LLM 调用，解析 JSON 返回。"""
        import concurrent.futures

        model = self._get_model()
        history_text = self._build_history_context(history)
        max_chars = int(get_config("intent_max_query_chars", 500))
        query_truncated = query[:max_chars]

        prompt = self._prompt_loader.load(
            "intent_classifier",
            history=history_text,
            query=query_truncated,
        )
        if not prompt:
            logger.warning("【意图识别】prompt 模板为空，降级")
            return None

        # 带超时的 LLM 调用
        timeout = int(get_config("intent_timeout", 5))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(model.invoke, prompt)
                response = future.result(timeout=timeout)
            raw = response.content if hasattr(response, "content") else str(response)
        except concurrent.futures.TimeoutError:
            logger.warning(f"【意图识别】LLM 调用超时 ({timeout}s)，降级")
            return None

        logger.debug(f"【意图识别】LLM 原始响应: {raw[:300]}")
        return self._parse_response(raw, query)

    def _parse_response(self, raw: str, query: str) -> Optional[IntentResult]:
        """解析 LLM 返回的 JSON，构建 IntentResult。"""
        # 提取 JSON（LLM 可能会包裹在 ```json 代码块中）
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            logger.warning(f"【意图识别】无法从响应中提取 JSON: {raw[:200]}")
            return None

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            logger.warning(f"【意图识别】JSON 解析失败: {e}, raw={raw[:200]}")
            return None

        intent = data.get("intent", "NEED_RAG")
        confidence = float(data.get("confidence", 0.5))
        reason = data.get("reason", "")
        search_query = data.get("search_query", query)

        # 规范化 intent 值
        if intent not in ("NEED_RAG", "NO_RAG"):
            intent = "NEED_RAG"

        # 构建 action_directive
        action_directive = self._build_directive(intent, reason, search_query)

        return IntentResult(
            intent=intent,
            confidence=confidence,
            reason=reason,
            search_query=search_query,
            action_directive=action_directive,
        )

    def _build_directive(
        self, intent: str, reason: str, search_query: str,
    ) -> str:
        """构建注入 Agent system prompt 的硬约束指令块。"""
        if intent == "NEED_RAG":
            return (
                "[SYSTEM OVERRIDE — 本次对话硬约束]\n"
                f"意图判定: 需要检索知识库\n"
                f"原因: {reason}\n"
                f"预检索查询: {search_query}\n"
                "执行指令: 必须首先调用 knowledge_search 工具进行检索，"
                "禁止跳过检索直接作答。知识库无结果时可调用 web_search 补充。"
            )
        else:
            return (
                "[SYSTEM OVERRIDE — 本次对话硬约束]\n"
                f"意图判定: 无需检索知识库\n"
                f"原因: {reason}\n"
                "执行指令:\n"
                "  跳过 knowledge_search。根据 query 性质决定后续行为：\n"
                "  - 需要实时/外部/最新信息（天气、新闻、股价、最新动态等）"
                "→ 必须调用 web_search 获取后作答\n"
                "  - 闲聊、问候、能力问询、纯常识问题 → 直接作答"
            )

    def _build_history_context(self, history: list | None) -> str:
        """将 LangChain 消息列表转为意图分类用的精简上下文。"""
        if not history:
            return "（无历史对话，这是首轮对话）"

        max_chars = int(get_config("intent_max_history_chars", 500))
        turns = int(get_config("intent_history_turns", 2))
        recent = history[-(turns * 2):]  # 每轮 user+assistant 两条

        lines = []
        for msg in recent:
            role = "用户" if msg.__class__.__name__ == "HumanMessage" else "助手"
            content = ""
            if hasattr(msg, "content"):
                content = str(msg.content) if msg.content else ""
            else:
                content = str(msg)
            content = content[:max_chars]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _fallback_result(self, reason: str) -> IntentResult:
        """降级结果：默认 NEED_RAG，无 action_directive（走原有逻辑）。"""
        return IntentResult(
            intent="NEED_RAG",
            confidence=0.5,
            reason=f"降级: {reason}",
            search_query="",
            action_directive="",
        )

    @staticmethod
    def _cache_key(query: str, session_id: str) -> str:
        raw = f"{session_id}:{query.strip()}"
        return hashlib.md5(raw.encode()).hexdigest()


def get_intent_classifier() -> IntentClassifier:
    """获取 IntentClassifier 单例。"""
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier
