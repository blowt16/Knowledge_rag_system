"""RAG 核心服务 — HyDE → 混合检索 → 重排序 → LLM 摘要。"""
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.rag.retrievers.query_rewriter import (
    get_retrieval_strategy, hyde_rewrite, simple_rewrite,
)
from app.rag.retrievers.hybrid_retriever import HybridRetriever
from app.rag.reorder_service import ReorderService
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

        # 步骤1: 策略判定
        try:
            strategy_info = get_retrieval_strategy(query, chat_history)
            need_rw = strategy_info["need_rewrite"]
            strategy = strategy_info["strategy"]
        except Exception as e:
            logger.error(f"【RAG】策略判定失败: {e}")
            need_rw = False
            strategy = "hybrid"

        logger.info(f"【RAG】开始处理查询: {query}, 策略: {strategy}")

        # 步骤2: HyDE 查询改写
        rewritten_query = None
        if need_rw:
            try:
                rewritten_query = await hyde_rewrite(query, chat_history)
            except Exception as e:
                logger.error(f"【HyDE】HyDE 改写失败: {e}, 使用简化改写")
                rewritten_query = simple_rewrite(query, chat_history)

        # 步骤3: 混合检索
        try:
            merged_docs, raw = await self._hybrid_retriever.retrieve(
                query=query, user_id=user_id,
                rewritten_query=rewritten_query, strategy=strategy)
        except Exception as e:
            logger.error(f"【RAG】混合检索失败: {e}")
            merged_docs = []

        if not merged_docs:
            logger.info("【RAG】未检索到知识库文档")
            return {"answer": "", "documents": [], "rewritten_query": rewritten_query or query}

        logger.info(f"【RAG】检索到 {len(merged_docs)} 个文档")

        # 步骤4: 重排序
        try:
            reranked = self._reorder_svc.rerank(
                rewritten_query or query, merged_docs, top_k)
            logger.info(f"【RAG】文档重排序成功，返回 {len(reranked)} 个文档")
        except Exception as e:
            logger.error(f"【RAG】重排序失败: {e}")
            reranked = merged_docs[:top_k]

        # 检索结果汇总（始终输出）
        summary_lines = [
            f"原始查询: {query}",
            f"改写查询: {rewritten_query or '（未改写）'}",
            f"检索策略: {strategy}",
        ]
        for i, doc in enumerate(reranked):
            src = doc.metadata.get("original_filename", "未知")
            content = doc.page_content[:120].replace("\n", " ")
            summary_lines.append(f"  文档{i+1}[{src}]: {content}...")
        logger.info("【RAG】检索结果汇总:\n" + "\n".join(summary_lines))

        # 步骤5: LLM 摘要
        try:
            answer = await self._generate_summary(
                query=query, documents=reranked, chat_history=chat_history,
                rewritten_query=rewritten_query)
        except Exception as e:
            logger.error(f"【RAG】生成摘要失败: {e}")
            answer = self._format_docs(reranked)

        return {
            "answer": answer, "documents": reranked,
            "rewritten_query": rewritten_query or query,
        }

    async def _generate_summary(self, query: str, documents: list,
                                chat_history: list = None,
                                rewritten_query: str = None) -> str:
        if not documents:
            return ""

        try:
            from app.core.background_init import init_manager
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.output_parsers import StrOutputParser

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

            prompt = ChatPromptTemplate.from_messages([
                ("system", "你是一个 RAG 知识库助手。基于以下检索结果回答问题。如果检索结果不足以回答问题，请说明。回答要简洁明了。"),
                ("human", "用户问题：{query}\n\n知识库检索结果：\n{context}"),
            ])
            chain = prompt | llm | StrOutputParser()
            answer = await chain.ainvoke({"query": query, "context": context_text})
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
        """同步版 RAG 检索（nest_asyncio 保证事件循环安全）。"""
        import asyncio
        return asyncio.run(self.search(query, user_id, chat_history))
