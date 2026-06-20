"""联网搜索服务 — 桩模块（待配置 API Key 后启用）。"""
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class WebSearchService:
    """联网搜索服务。当前为桩实现，配置搜索 API Key 后启用真实搜索。"""

    def search(self, query: str) -> str:
        """执行联网搜索。

        Returns:
            搜索结果摘要文本
        """
        logger.debug(f"【联网搜索】桩调用: {query}")
        return f"联网搜索功能暂未配置 API Key，请使用知识库检索。（搜索词：{query}）"
