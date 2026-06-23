"""文本切分器 — RecursiveCharacterTextSplitter + 可选语义合并。"""
import os
import asyncio
import threading
from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class SemanticMergeModel:
    """语义合并 SentenceTransformer 模型 — 双重检查锁定单例，全局复用，线程安全。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._model = None
        return cls._instance

    def _get_model(self):
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model
            model_name = os.getenv("SEMANTIC_MERGE_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            logger.info(f"【语义合并】SentenceTransformer '{model_name}' 已加载")
            return self._model

    def encode(self, sentences: list[str]) -> list:
        return self._get_model().encode(sentences)

    def warmup(self):
        """预加载模型（启动时调用，避免首次切分卡顿）。"""
        self._get_model()


def _format_page_range(pages: list) -> str:
    """将页码列表格式化为范围字符串，如 [1,2,3] → '1-3', [1,3,5] → '1,3,5'。"""
    if not pages:
        return ""
    sorted_pages = sorted(set(pages))
    ranges = []
    start = sorted_pages[0]
    end = start
    for p in sorted_pages[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append(f"{start}-{end}" if end > start else str(start))
            start = p
            end = p
    ranges.append(f"{start}-{end}" if end > start else str(start))
    return ",".join(ranges)


class AsyncTextSplitter:
    """异步文本切分器 — 封装 RecursiveCharacterTextSplitter。"""

    def __init__(self):
        self._chunk_size = get_config("chunk_size", 500)
        self._chunk_overlap = get_config("chunk_overlap", 50)
        self._separators = get_config("separators", ["\n\n", "\n", "。", "！", "？", "!", "?", " ", ""])

    def _create_splitter(self):
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        return RecursiveCharacterTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            separators=self._separators,
            length_function=len,
        )

    def split_documents(self, documents: list, enable_semantic_merge: bool | None = None) -> list:
        if not documents:
            return []
        if enable_semantic_merge is None:
            enable_semantic_merge = get_config("enable_semantic_merge", False)
        splitter = self._create_splitter()
        result = splitter.split_documents(documents)
        if enable_semantic_merge and len(result) > 1:
            result = self._merge_documents(result)
        for i, doc in enumerate(result):
            doc.metadata["chunk_index"] = i
        logger.debug(f"【文本切分】{len(documents)} 个文档 → {len(result)} 个 chunk")
        return result

    def _merge_documents(self, documents: list) -> list:
        texts = [doc.page_content for doc in documents]
        merged_texts = self._semantic_merge(texts)
        if len(merged_texts) == len(documents):
            return documents
        merged_docs = []
        di = 0
        for mt in merged_texts:
            start_di = di
            accumulated = ""
            pages = []
            image_paths = []
            has_images = False
            while di < len(documents) and len(accumulated) < len(mt):
                accumulated += documents[di].page_content
                p = documents[di].metadata.get("page")
                if p is not None and p not in pages:
                    pages.append(p)
                ips = documents[di].metadata.get("image_paths", [])
                for ip in ips:
                    if ip not in image_paths:
                        image_paths.append(ip)
                if documents[di].metadata.get("has_images"):
                    has_images = True
                di += 1
            merged = documents[start_di] if start_di < len(documents) else documents[0]
            merged.page_content = mt
            # 聚合溯源元数据
            if pages:
                merged.metadata["page"] = _format_page_range(pages)
            if image_paths:
                merged.metadata["image_paths"] = image_paths
            if has_images:
                merged.metadata["has_images"] = True
            merged_docs.append(merged)
        return merged_docs

    async def async_split_documents(self, documents: list) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.split_documents, documents)

    def split_text(self, text: str, enable_semantic_merge: bool = False) -> list[str]:
        if not text:
            return []
        splitter = self._create_splitter()
        chunks = splitter.split_text(text)

        if enable_semantic_merge and len(chunks) > 1:
            chunks = self._semantic_merge(chunks)

        return chunks

    def _semantic_merge(self, chunks: list[str]) -> list[str]:
        threshold = get_config("semantic_merge_threshold", 0.7)
        max_len = get_config("max_merge_len", 1500)
        try:
            import numpy as np
            model = SemanticMergeModel()
            embeddings = model.encode(chunks)
            merged = [chunks[0]]
            current_emb = embeddings[0]

            for i in range(1, len(chunks)):
                can_merge = len(chunks[i]) + len(merged[-1]) <= max_len
                if can_merge:
                    sim = float(np.dot(current_emb, embeddings[i]) / (np.linalg.norm(current_emb) * np.linalg.norm(embeddings[i]) + 1e-9))
                else:
                    sim = 0.0  # 超过长度上限，强制不合并
                if sim > threshold:
                    merged[-1] += chunks[i]
                    current_emb = (current_emb + embeddings[i]) / 2
                else:
                    merged.append(chunks[i])
                    current_emb = embeddings[i]
            return merged
        except Exception as e:
            logger.warning(f"【文本切分】语义合并失败: {e}")
            return chunks
