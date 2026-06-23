"""混合检索器 — BM25 + 向量并行检索 + RRF 排名融合。"""
import asyncio
import threading
from collections import OrderedDict
from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class _BM25IndexCache:
    """BM25 索引 LRU 缓存。"""

    def __init__(self, max_size: int = None):
        if max_size is None:
            max_size = get_config("bm25_cache_size", 20)
        self._cache: OrderedDict[str, object] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, user_id: str):
        with self._lock:
            if user_id in self._cache:
                self._cache.move_to_end(user_id)
                return self._cache[user_id]
        return None

    def set(self, user_id: str, index: object):
        with self._lock:
            if user_id in self._cache:
                self._cache.move_to_end(user_id)
            else:
                self._cache[user_id] = index
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)

    def invalidate(self, user_id: str):
        with self._lock:
            if user_id in self._cache:
                del self._cache[user_id]
                logger.debug(f"【混合检索】用户 {user_id} BM25 缓存已失效")


_bm25_cache = _BM25IndexCache()


class HybridRetriever:
    """混合检索：BM25（关键词）+ 向量（语义），RRF 融合。"""

    def __init__(self, k: int = None):
        self._k = k if k is not None else get_config("k", 5)

    def _get_or_build_bm25(self, user_id: str, k: int = None):
        if k is None:
            k = get_config("bm25_recall_k", 10)
        cached = _bm25_cache.get(user_id)
        if cached is not None:
            return cached

        from app.rag.vector_store import VectorStoreService
        vs = VectorStoreService()
        collection = vs._get_collection()
        results = collection.get(where={"user_id": user_id}, include=["metadatas", "documents"])

        documents = results.get("documents", [])
        metadatas = results.get("metadatas", [])
        if not documents:
            return None

        from langchain_community.retrievers import BM25Retriever
        from langchain_core.documents import Document

        docs = []
        for i, text in enumerate(documents):
            meta = metadatas[i] if i < len(metadatas) else {}
            docs.append(Document(page_content=text, metadata=meta))
        bm25 = BM25Retriever.from_documents(docs, k=k)

        _bm25_cache.set(user_id, bm25)
        logger.debug(f"【混合检索】BM25 索引已构建: {len(documents)} 条文档, k={k}")
        return bm25

    async def bm25_search(self, query: str, user_id: str) -> list:
        """独立的 BM25 关键词检索（供上游并行调度）。"""
        bm25_recall = get_config("bm25_recall_k", 10)
        bm25 = self._get_or_build_bm25(user_id, k=bm25_recall)
        if bm25 is None:
            return []
        return await bm25.ainvoke(query)

    async def retrieve(self, query: str, user_id: str,
                       rewritten_query: str = None,
                       strategy: str = "hybrid",
                       bm25_results: list = None) -> tuple[list, list]:
        if not user_id:
            return [], []

        from app.rag.vector_store import VectorStoreService
        vs = VectorStoreService()

        has_precomputed = bm25_results is not None
        if bm25_results is None:
            bm25_results = []
        vector_results = []

        if strategy == "bm25_only":
            if not has_precomputed:
                bm25_recall = get_config("bm25_recall_k", 10)
                bm25 = self._get_or_build_bm25(user_id, k=bm25_recall)
                if bm25 is not None:
                    bm25_results = await bm25.ainvoke(query)
        elif strategy in ("hybrid", "hybrid_rewritten"):
            vec_query = rewritten_query if rewritten_query and strategy == "hybrid_rewritten" else query
            vector_recall = get_config("vector_recall_k", 10)

            if has_precomputed:
                # BM25 已在 rag_service 中预计算（与 HyDE 并行），只跑向量
                vector_results = vs.similarity_search(vec_query, user_id, vector_recall)
            else:
                # 并行 BM25 + 向量
                bm25_recall = get_config("bm25_recall_k", 10)
                bm25 = self._get_or_build_bm25(user_id, k=bm25_recall)

                async def _bm25():
                    if bm25 is None:
                        return []
                    return await bm25.ainvoke(query)

                async def _vector():
                    return vs.similarity_search(vec_query, user_id, vector_recall)

                bm25_results, vector_results = await asyncio.gather(
                    _bm25(), _vector())

        merged = self._rrf_fusion(bm25_results, vector_results)
        logger.info(f"【混合检索】BM25: {len(bm25_results)} + 向量: {len(vector_results)} → RRF 融合: {len(merged)}")
        return merged, {"bm25": bm25_results, "vector": vector_results}

    def _rrf_fusion(self, bm25_docs: list, vector_docs: list, k: int = None) -> list:
        if k is None:
            k = get_config("rrf_constant", 60)

        scores = {}
        for rank, doc in enumerate(bm25_docs, start=1):
            doc_id = doc.metadata["chunk_id"]
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank)
        for rank, doc in enumerate(vector_docs, start=1):
            doc_id = doc.metadata["chunk_id"]
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank)

        sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        seen = set()
        merged = []
        for doc_id, score in sorted_ids:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            for doc in bm25_docs + vector_docs:
                d_id = doc.metadata["chunk_id"]
                if d_id == doc_id and id(doc) not in seen:
                    doc.metadata["rrf_score"] = score
                    merged.append(doc)
                    seen.add(id(doc))
                    break
        return merged

    @staticmethod
    def invalidate_cache(user_id: str):
        _bm25_cache.invalidate(user_id)
