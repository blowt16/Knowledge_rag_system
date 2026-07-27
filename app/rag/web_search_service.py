"""联网搜索服务 — 基于 DuckDuckGo 的免费搜索实现。"""
from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class WebSearchService:
    """联网搜索服务。使用 DuckDuckGo 搜索，无需 API Key。"""

    _DEFAULT_TIMEOUT = 10
    _DEFAULT_MAX_RESULTS = 5

    def search(self, query: str) -> str:
        """执行联网搜索，返回格式化结果。

        Returns:
            搜索结果文本，包含标题、摘要、URL。
        """
        max_results = int(get_config("web_search_max_results", self._DEFAULT_MAX_RESULTS))
        timeout = int(get_config("web_search_timeout", self._DEFAULT_TIMEOUT))

        logger.info(f"【联网搜索】查询: {query[:100]}")

        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results, timelimit=None))
        except Exception as e:
            logger.error(f"【联网搜索】搜索失败: {e}")
            return (
                f"联网搜索暂时不可用。建议通过以下方式获取信息：\n"
                f"1. 搜索引擎直接搜索「{query}」\n"
                f"2. 访问相关专业网站\n"
                f"3. 稍后重试联网搜索"
            )

        if not results:
            logger.info(f"【联网搜索】无结果: {query[:100]}")
            return f"未搜索到与「{query}」相关的结果。"

        logger.info(f"【联网搜索】返回 {len(results)} 条结果")

        lines = [f"联网搜索结果（共 {len(results)} 条）：\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")
            body = r.get("body", "")
            href = r.get("href", "")
            body_trimmed = body[:300]
            lines.append(f"[{i}] {title}\n    {body_trimmed}\n    URL: {href}\n")

        return "\n".join(lines)
