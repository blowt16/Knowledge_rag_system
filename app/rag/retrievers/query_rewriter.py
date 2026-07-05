"""查询改写与必要性分类器 — 两层判定 + HyDE 生成假设性文档。"""
import os
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


def _get_q_words() -> list[str]:
    return get_config("query_words", ["什么", "怎么", "如何", "为什么", "哪", "谁",
                                       "多少", "吗", "呢", "？", "?", "能不能", "可不可以"])


def _get_pro_words() -> list[str]:
    return get_config("pronoun_words", ["它", "他", "她", "这个", "那个", "这些", "那些",
                                         "它的", "他的", "她的", "这", "那", "上面", "前面", "刚才"])


def preprocess_query(query: str) -> str:
    """清洗原始 Query（仅去空格，用于分类器判定）。"""
    return query.strip().replace(' ', '')


def is_pure_keyword(query: str) -> bool:
    """第一层：极简关键词判定。"""
    cleaned = preprocess_query(query)
    q_words = _get_q_words()
    pro_words = _get_pro_words()

    if any(w in cleaned for w in q_words) or any(w in cleaned for w in pro_words):
        return False

    max_len = get_config("pure_keyword_max_length", 10)
    return len(cleaned) <= max_len


def need_rewrite(query: str, conversation_history: list = None) -> bool:
    """判断是否需要调用 HyDE 改写。

    仅在查询包含疑问词或代词时才改写——这些词表明查询是自然语言问题，
    需要 HyDE 扩展上下文。纯关键字查询不会被改写。
    """
    if is_pure_keyword(query):
        return False

    cleaned = preprocess_query(query)

    if any(w in cleaned for w in _get_pro_words()):
        return True
    if any(w in cleaned for w in _get_q_words()):
        return True

    return False


def get_retrieval_strategy(query: str, conversation_history: list = None) -> dict:
    """返回检索策略。"""
    if is_pure_keyword(query):
        return {"need_rewrite": False, "is_pure_keyword": True, "strategy": "bm25_only"}

    need = need_rewrite(query, conversation_history)
    strategy = "hybrid_rewritten" if need else "hybrid"
    return {"need_rewrite": need, "is_pure_keyword": False, "strategy": strategy}


async def hyde_rewrite(query: str, chat_history: list = None) -> str:
    """HyDE: 生成假设性文档用于向量检索匹配。"""
    try:
        loader = PromptLoader()
        history_text = _format_chat_history(chat_history or [])
        prompt = loader.load("hyde", query=query, chat_history=history_text)

        from app.core.background_init import init_manager
        llm = init_manager.chat_model
        if llm is None:
            logger.warning("【HyDE】LLM 未就绪，使用原始查询")
            return query

        response = await llm.ainvoke(prompt)
        rewritten = response.content if hasattr(response, "content") else str(response)
        rewritten = rewritten.strip()

        min_len = get_config("hyde_min_length", 3)
        if not rewritten or len(rewritten) < min_len:
            logger.warning("【HyDE】生成结果过短，使用原始查询")
            return query

        logger.info(f"【HyDE】查询改写完成:\n原始查询: {query}\n改写结果: {rewritten}")
        return rewritten

    except Exception as e:
        logger.error(f"【HyDE】生成假设性文档失败: {e}")
        return query


def _format_chat_history(history: list) -> str:
    """将消息列表格式化为文本，由 llm_history_turns 控制轮次。"""
    if not history:
        return "无"
    max_turns = get_config("llm_history_turns", 5)
    lines = []
    for msg in history[-(max_turns * 2):]:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))
        prefix = "用户" if role == "human" else "助手" if role == "ai" else role
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


def simple_rewrite(query: str, chat_history: list = None) -> str:
    """简化改写（非 HyDE）。"""
    history_text = _format_chat_history(chat_history or [])
    if chat_history and history_text != "无":
        return f"对话历史：{history_text}\n当前问题：{query}"
    return query
