"""联网搜索服务 — 基于 DuckDuckGo 的免费搜索实现。"""
import re
from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class WebSearchService:
    """联网搜索服务。使用 DuckDuckGo 搜索，无需 API Key。

    默认仅启用国内可访问的后端（mojeek, yandex），
    可通过 web_search_backends 环境变量覆盖。
    """

    _DEFAULT_RESULTS = 5
    _DEFAULT_TIMEOUT = 8
    _MIN_BODY_LENGTH = 30
    _MAX_BODY_LENGTH = 200
    # 国内网络可稳定访问的搜索引擎
    _DEFAULT_BACKENDS = "mojeek,yandex"

    # 低质量域名模式（钓鱼/垃圾站）
    _SPAM_DOMAIN_RE = re.compile(
        r'\.(xyz|top|tk|ml|ga|cf|gq)$|'
        r'(ararlluf|bit\.ly|tinyurl|ow\.ly|shorte\.st)',
        re.IGNORECASE,
    )

    def search(self, query: str) -> str:
        """执行联网搜索，返回格式化结果。

        Returns:
            搜索结果文本，包含标题、摘要、URL。
        """
        max_results = int(get_config("web_search_max_results", self._DEFAULT_RESULTS))
        timeout = int(get_config("web_search_timeout", self._DEFAULT_TIMEOUT))
        backends = get_config("web_search_backends", self._DEFAULT_BACKENDS)
        # 多取一些原始结果，过滤后保证输出数量
        raw_limit = max_results * 2 + 3

        logger.info(
            f"【联网搜索】查询: {query[:100]}, backends={backends}, "
            f"max_results={max_results}, timeout={timeout}s"
        )

        # 1. 搜索
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                raw_results = list(ddgs.text(
                    query,
                    max_results=raw_limit,
                    timelimit="y",
                    backend=backends,
                ))
        except Exception as e:
            logger.error(f"【联网搜索】搜索失败: {e}")
            return (
                f"联网搜索暂时不可用。建议通过以下方式获取信息：\n"
                f"1. 搜索引擎直接搜索「{query}」\n"
                f"2. 访问相关专业网站\n"
                f"3. 稍后重试联网搜索"
            )

        # 2. 过滤低质量结果
        filtered = []
        for r in raw_results:
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            # 跳过空内容、过短内容、垃圾域名
            if len(body) < self._MIN_BODY_LENGTH:
                continue
            if self._SPAM_DOMAIN_RE.search(href):
                logger.debug(f"【联网搜索】过滤垃圾域名: {href}")
                continue
            filtered.append(r)

        if not filtered:
            logger.info(f"【联网搜索】过滤后无有效结果: raw={len(raw_results)}")
            return f"未搜索到与「{query}」相关的有效结果，请尝试更换搜索词。"

        # 3. 截取所需数量
        results = filtered[:max_results]
        logger.info(f"【联网搜索】raw={len(raw_results)}, filtered={len(filtered)}, final={len(results)}")

        # 4. 格式化输出
        lines = [f"联网搜索结果（共 {len(results)} 条）：\n"]
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "无标题").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            body_trimmed = body[:self._MAX_BODY_LENGTH]
            lines.append(f"[{i}] {title}\n    {body_trimmed}\n    URL: {href}\n")

        return "\n".join(lines)
