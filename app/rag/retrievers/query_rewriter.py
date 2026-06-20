"""查询改写与必要性分类器 — 两层判定 + HyDE 生成假设性文档。"""
import os
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)

Q_WORDS = ['什么', '怎么', '如何', '为什么', '哪', '谁',
           '多少', '吗', '呢', '？', '?', '能不能', '可不可以']
PRO_WORDS = ['它', '他', '她', '这个', '那个', '这些', '那些',
             '它的', '他的', '她的', '这', '那', '上面', '前面', '刚才']


def preprocess_query(query: str) -> str:
    """清洗原始 Query（仅去空格，用于分类器判定）。"""
    return query.strip().replace(' ', '')


def is_pure_keyword(query: str) -> bool:
    """第一层：极简关键词判定。"""
    cleaned = preprocess_query(query)

    has_q = any(w in cleaned for w in Q_WORDS)
    has_pro = any(w in cleaned for w in PRO_WORDS)
    if has_q or has_pro:
        return False

    return len(cleaned) <= 6


def need_rewrite(query: str, conversation_history: list = None) -> bool:
    """判断是否需要调用 HyDE 改写。

    两层结构：
    1. 纯关键词 → 不改写，仅 BM25
    2. 三规则 → 任一满足则改写
    """
    if is_pure_keyword(query):
        return False

    cleaned = preprocess_query(query)

    # 规则 A：存在代词
    if any(w in cleaned for w in PRO_WORDS):
        return True

    # 规则 B：存在疑问词
    if any(w in cleaned for w in Q_WORDS):
        return True

    # 规则 C：多轮对话中简短追问（兜底）
    if conversation_history and len(cleaned) < 15:
        return True

    return False


def get_retrieval_strategy(query: str, conversation_history: list = None) -> dict:
    """返回检索策略：纯关键词/不改写/需改写，及对应的检索方式。

    Returns:
        {"need_rewrite": bool, "is_pure_keyword": bool, "strategy": "bm25_only"|"hybrid"|"hybrid_rewritten"}
    """
    if is_pure_keyword(query):
        return {"need_rewrite": False, "is_pure_keyword": True, "strategy": "bm25_only"}

    need = need_rewrite(query, conversation_history)
    strategy = "hybrid_rewritten" if need else "hybrid"
    return {"need_rewrite": need, "is_pure_keyword": False, "strategy": strategy}


async def hyde_rewrite(query: str, chat_history: list = None) -> str:
    """HyDE: 生成假设性文档用于向量检索匹配。

    调用 LLM 生成一段假设可能出现在知识库中的陈述句文档片段。
    """
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

        if not rewritten or len(rewritten) < 3:
            logger.warning("【HyDE】生成结果过短，使用原始查询")
            return query

        logger.info(f"【HyDE】生成的假设性文档: {rewritten[:100]}...")
        return rewritten

    except Exception as e:
        logger.error(f"【HyDE】生成假设性文档失败: {e}")
        return query


def _format_chat_history(history: list) -> str:
    """将消息列表格式化为文本。"""
    if not history:
        return "无"
    lines = []
    for msg in history:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))
        prefix = "用户" if role == "human" else "助手" if role == "ai" else role
        lines.append(f"{prefix}: {content[:200]}")
    return "\n".join(lines)


def simple_rewrite(query: str, chat_history: list = None) -> str:
    """简化改写（非 HyDE）：规则替换代词 + 拼接上下文。"""
    cleaned = preprocess_query(query)
    history_text = _format_chat_history(chat_history or [])

    if chat_history and history_text != "无":
        return f"对话历史：{history_text}\n当前问题：{query}"
    return query
