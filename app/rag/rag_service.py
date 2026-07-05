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
        k = get_config("k", 5)
        self._hybrid_retriever = HybridRetriever(k=k)
        self._reorder_svc = ReorderService()

    async def search(self, query: str, user_id: str = "",
                     chat_history: list = None, top_k: int = None,
                     on_chunk=None, skip_summary: bool = None) -> dict:
        """RAG 检索。若 on_chunk 回调传入则流式推送 token。

        skip_summary 默认从 chroma.yaml 读取（默认 true），跳过 LLM 摘要直接返回原始文档。"""
        if skip_summary is None:
            skip_summary = get_config("skip_summary", True)
        if top_k is None:
            top_k = get_config("k", 5)

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

        # 步骤2: BM25 (仅 hybrid_rewritten 时预计算, 与 HyDE 并行)
        import asyncio
        bm25_task = None
        if strategy == "hybrid_rewritten":
            bm25_task = asyncio.create_task(
                self._hybrid_retriever.bm25_search(query, user_id))

        # 步骤2b: HyDE 查询改写 (与 BM25 并行)
        rewritten_query = None
        if need_rw:
            try:
                rewritten_query = await hyde_rewrite(query, chat_history)
            except Exception as e:
                logger.error(f"【HyDE】HyDE 改写失败: {e}, 使用简化改写")
                rewritten_query = simple_rewrite(query, chat_history)

        # 等待 BM25 完成 (若 HyDE 耗时 > BM25 则早已完成无需等待)
        bm25_results = await bm25_task if bm25_task else None

        # 步骤3: 向量检索 + RRF 融合 (BM25 预计算结果传入)
        try:
            merged_docs, raw = await self._hybrid_retriever.retrieve(
                query=query, user_id=user_id,
                rewritten_query=rewritten_query, strategy=strategy,
                bm25_results=bm25_results)
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
                query, merged_docs, top_k)
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

        # 步骤5: LLM 摘要 — skip_summary 时直接返回原始文档，省一次 LLM 调用
        if skip_summary:
            answer = self._format_docs(reranked)
            img_md_lines = self._build_image_markdown(reranked)
            if img_md_lines:
                img_block = "\n\n---\n**📷 相关图片（仅展示以下图片，禁止编造不存在页码的图片链接）：**\n\n" + "\n\n".join(img_md_lines)
                answer += img_block
                logger.info(f"【RAG】图片注入: {len(img_md_lines)} 张\n  " + "\n  ".join(img_md_lines))
            if on_chunk:
                await on_chunk(answer)
            return {
                "answer": answer, "documents": reranked,
                "rewritten_query": rewritten_query or query,
                "chunks": [answer],
            }

        try:
            answer, chunks = await self._generate_summary(
                query=query, documents=reranked, chat_history=chat_history,
                rewritten_query=rewritten_query, on_chunk=on_chunk)
        except Exception as e:
            logger.error(f"【RAG】生成摘要失败: {e}")
            answer = self._format_docs(reranked)
            chunks = [answer]

        return {
            "answer": answer, "documents": reranked,
            "rewritten_query": rewritten_query or query,
            "chunks": chunks,
        }

    @staticmethod
    def _build_image_markdown(documents: list) -> list[str]:
        """从文档元数据中提取图片路径，构建 Markdown 图片链接。"""
        from app.utils.path_tool import get_server_url
        base_url = get_server_url()
        img_lines = []
        seen = set()
        for doc in documents:
            label = doc.metadata.get("original_filename", "未知")
            for img_path in doc.metadata.get("image_paths", []):
                # 统一为前向斜杠，去掉 image_extract_dir 前缀
                relative = img_path.replace("\\", "/")
                from app.config.loader import get_config as _cfg
                prefix = _cfg("image_extract_dir", "extracted_images") + "/"
                if relative.startswith(prefix):
                    relative = relative[len(prefix):]
                if relative not in seen:
                    seen.add(relative)
                    img_lines.append(f"![{label}]({base_url}/images/{relative})")
        return img_lines

    async def _generate_summary(self, query: str, documents: list,
                                chat_history: list = None,
                                rewritten_query: str = None,
                                on_chunk=None) -> tuple[str, list[str]]:
        """返回 (完整回答, token_chunks)。有 on_chunk 回调时流式推送。"""
        if not documents:
            return "", []

        try:
            from app.core.background_init import init_manager
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.output_parsers import StrOutputParser

            llm = init_manager.chat_model
            if llm is None:
                fallback = self._format_docs(documents)
                return fallback, [fallback]

            max_chars = get_config("chunk_size", 500)
            contexts = []
            for i, doc in enumerate(documents):
                meta = doc.metadata
                source = meta.get("original_filename", "未知")
                page = meta.get("page", "")
                chapter = meta.get("current_chapter", "")
                image_paths = meta.get("image_paths", [])
                ctx = f"[来源: {source}"
                if page:
                    ctx += f", 第{page}页"
                if chapter:
                    ctx += f", {chapter}"
                ctx += f"]\n{doc.page_content[:max_chars]}"
                # 将图片 URL 注入上下文，让 LLM 知道有可用图片
                if image_paths:
                    img_md = self._build_image_markdown([doc])
                    if img_md:
                        ctx += "\n\n--- 可用的相关图片（仅使用以下图片，禁止编造不存在页码的链接） ---\n" + "\n".join(img_md)
                contexts.append(ctx)
            context_text = "\n\n---\n\n".join(contexts)

            history_text = self._format_history(chat_history)

            from app.utils.prompt_loader import PromptLoader
            prompt_text = PromptLoader().load(
                "rag_answer", query=query, history=history_text, context=context_text)
            prompt = ChatPromptTemplate.from_messages([
                ("human", "{input}"),
            ])
            chain = prompt | llm | StrOutputParser()
            answer = ""
            chunks = []
            async for chunk in chain.astream({"input": prompt_text}):
                if chunk:
                    answer += chunk
                    chunks.append(chunk)
                    if on_chunk:
                        await on_chunk(chunk)

            # LLM 回答后追加图片 Markdown，确保图片始终展示
            img_md_lines = self._build_image_markdown(documents)
            if img_md_lines:
                img_block = "\n\n---\n**📷 相关图片（仅展示以下图片，禁止编造不存在页码的图片链接）：**\n\n" + "\n\n".join(img_md_lines)
                answer += img_block
                chunks.append(img_block)
                if on_chunk:
                    await on_chunk(img_block)
                logger.info(f"【RAG】图片注入: {len(img_md_lines)} 张\n  " + "\n  ".join(img_md_lines))

            return answer.strip(), chunks
        except Exception as e:
            logger.error(f"【RAG】生成摘要失败: {e}")
            fallback = self._format_docs(documents)
            return fallback, [fallback]

    @staticmethod
    def _format_history(chat_history: list = None) -> str:
        if not chat_history:
            return "无"
        import re
        max_turns = get_config("llm_history_turns", 5)
        lines = []
        for msg in chat_history[-(max_turns * 2):]:
            role = getattr(msg, "type", "unknown")
            content = getattr(msg, "content", str(msg))
            # 移除旧格式 [文档N] 引用，避免多轮对话中跨轮次幻觉
            content = re.sub(r'\[文档\d+\]', '', content)
            prefix = "用户" if role == "human" else "助手" if role == "ai" else role
            lines.append(f"{prefix}: {content}")
        return "\n".join(lines)

    def _format_docs(self, documents: list) -> str:
        if not documents:
            return ""
        max_chars = get_config("chunk_size", 500)
        lines = []
        for i, doc in enumerate(documents):
            source = doc.metadata.get("original_filename", "未知")
            page = doc.metadata.get("page", "")
            chapter = doc.metadata.get("current_chapter", "")
            header = f"[来源: {source}"
            if page:
                header += f", 第{page}页"
            if chapter:
                header += f", {chapter}"
            header += "]"
            lines.append(f"{header}\n{doc.page_content[:max_chars]}")
        return "\n\n---\n\n".join(lines)
