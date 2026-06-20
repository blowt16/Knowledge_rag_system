"""RAG 核心服务 — HyDE → 混合检索 → 重排序 → LLM 摘要。"""
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.rag.retrievers.query_rewriter import (
    get_retrieval_strategy, hyde_rewrite, simple_rewrite,
)
from app.rag.retrievers.hybrid_retriever import HybridRetriever
from app.rag.reorder_service import ReorderService
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


class RAGService:
    """RAG 核心服务：完整检索-摘要管线。"""

    def __init__(self):
        k = get_config("k", 3)
        self._hybrid_retriever = HybridRetriever(k=k)
        self._reorder_svc = ReorderService()

    async def search(self, query: str, user_id: str = "",
                     chat_history: list = None, top_k: int = None) -> dict:
        if top_k is None:
            top_k = get_config("k", 3)

        if not user_id:
            logger.warning("【RAG】user_id 为空，不返回任何文档")
            return {"answer": "", "documents": [], "rewritten_query": ""}

        try:
            strategy_info = get_retrieval_strategy(query, chat_history)
            need_rw = strategy_info["need_rewrite"]
            strategy = strategy_info["strategy"]
            logger.info(f"【HyDE】开始处理查询: {query}, 策略: {strategy}")

            rewritten_query = None
            if need_rw:
                try:
                    rewritten_query = await hyde_rewrite(query, chat_history)
                except Exception as e:
                    logger.error(f"【HyDE】HyDE 改写失败: {e}, 使用简化改写")
                    rewritten_query = simple_rewrite(query, chat_history)

            merged_docs, raw = await self._hybrid_retriever.retrieve(
                query=query, user_id=user_id,
                rewritten_query=rewritten_query, strategy=strategy)

            if not merged_docs:
                logger.info("【HyDE】未检索到知识库文档")
                return {"answer": "", "documents": [], "rewritten_query": rewritten_query or query}

            logger.info(f"【HyDE】检索到 {len(merged_docs)} 个知识库文档")

            try:
                reranked = self._reorder_svc.rerank(
                    rewritten_query or query, merged_docs, top_k)
                logger.info(f"【RAG】文档重排序成功，返回 {len(reranked)} 个文档")
            except Exception as e:
                logger.error(f"【RAG】重排序失败: {e}")
                reranked = merged_docs[:top_k]

            answer = await self._generate_summary(
                query=query, documents=reranked, chat_history=chat_history,
                rewritten_query=rewritten_query)

            return {
                "answer": answer, "documents": reranked,
                "rewritten_query": rewritten_query or query,
            }
        except Exception as e:
            logger.error(f"【RAG】检索失败: {e}")
            return {"answer": "", "documents": [], "rewritten_query": ""}

    async def _generate_summary(self, query: str, documents: list,
                                chat_history: list = None,
                                rewritten_query: str = None) -> str:
        if not documents:
            return ""

        try:
            from app.core.background_init import init_manager
            llm = init_manager.chat_model
            if llm is None:
                return self._format_docs(documents)

            max_chars = get_config("summary_max_chars", 800)
            contexts = []
            for i, doc in enumerate(documents):
                meta = doc.metadata
                source = meta.get("original_filename", "未知")
                page = meta.get("page", "")
                ctx = f"[文档{i+1}] 来源: {source}"
                if page:
                    ctx += f", 第{page}页"
                ctx += f"\n{doc.page_content[:max_chars]}"
                contexts.append(ctx)

            context_text = "\n\n---\n\n".join(contexts)
            loader = PromptLoader()
            summary_prompt = loader.load("summary", content=context_text)
            prompt = f"用户问题：{query}\n\n知识库检索结果：\n{summary_prompt}\n\n请基于以上检索结果回答问题。如果检索结果不足以回答问题，请说明。回答要简洁明了。"

            response = await llm.ainvoke(prompt)
            answer = response.content if hasattr(response, "content") else str(response)
            return answer.strip()
        except Exception as e:
            logger.error(f"【RAG】生成摘要失败: {e}")
            return self._format_docs(documents)

    def _format_docs(self, documents: list) -> str:
        if not documents:
            return ""
        max_chars = get_config("fallback_max_chars", 500)
        lines = []
        for i, doc in enumerate(documents):
            source = doc.metadata.get("original_filename", "未知")
            lines.append(f"[{i+1}] 来源: {source}\n{doc.page_content[:max_chars]}")
        return "\n\n---\n\n".join(lines)

    def search_sync(self, query: str, user_id: str = "",
                    chat_history: list = None) -> dict:
        import asyncio
        return asyncio.run(self.search(query, user_id, chat_history))
