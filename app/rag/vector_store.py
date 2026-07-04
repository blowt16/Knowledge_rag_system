"""ChromaDB 向量存储 — 全局串行写入（threading.Lock），单进程单事件循环。"""
import threading
from langchain_chroma import Chroma
from app.config.loader import get_config
from app.utils.path_tool import resolve_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class VectorStoreService:
    """ChromaDB 单例管理 — 所有写入操作全局串行，跨请求/跨用户均互斥。"""

    _instance = None
    _init_lock = threading.Lock()
    _write_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._store = None
        return cls._instance

    @property
    def collection_name(self) -> str:
        return get_config("collection_name", "rag_collection")

    @property
    def k(self) -> int:
        return get_config("k", 5)

    @property
    def persist_directory(self) -> str:
        pd = get_config("persist_directory", "data/chromadb")
        return str(resolve_path(pd))

    def get_store(self) -> Chroma:
        """获取或创建 Chroma 向量存储（懒加载）。"""
        if self._store is None:
            from app.core.background_init import init_manager
            embed_fn = init_manager.embed_model
            if embed_fn is None:
                from app.utils.factory import create_embedding_model
                embed_fn = create_embedding_model()

            self._store = Chroma(
                collection_name=self.collection_name,
                embedding_function=embed_fn,
                persist_directory=self.persist_directory,
                collection_metadata={"hnsw:space": get_config("hnsw_space", "cosine")},
            )
            logger.info(f"【向量数据库】Chroma '{self.collection_name}' 已就绪")
        return self._store

    def _get_collection(self):
        """获取底层 ChromaDB collection（用于 delete 操作）。"""
        return self.get_store()._collection

    def add_documents(self, documents: list):
        """批量添加文档 — 全局串行，所有并发请求在此排队等待。"""
        if not documents:
            return
        with self._write_lock:
            ids = []
            for i, doc in enumerate(documents):
                cid = doc.metadata.get("chunk_id")
                if cid:
                    ids.append(cid)
                else:
                    ids.append(f"{doc.metadata.get('md5', 'unknown')}_{i}")
            self.get_store().add_documents(documents, ids=ids)
            logger.debug(f"【向量数据库】已入库 {len(documents)} 条文档")

    def similarity_search(self, query: str, user_id: str, k: int = None) -> list:
        """向量相似度检索，按 user_id 隔离 + 低于阈值过滤。"""
        if k is None:
            k = self.k

        threshold = get_config("vector_distance_threshold", 0.45)
        results = self.get_store().similarity_search_with_score(
            query, k=k, filter={"user_id": user_id},
        )

        docs = []
        for doc, score in results:
            if score > threshold > 0:
                continue
            doc.metadata["vector_score"] = float(score)
            docs.append(doc)

        filtered = len(results) - len(docs)
        if filtered:
            logger.debug(f"【向量数据库】距离阈值过滤: {filtered}/{len(results)} 条文档距离超过 {threshold}")
        return docs

    def delete_by_md5(self, user_id: str, md5: str):
        """按 MD5 删除文档，异常透传给上层处理回滚。"""
        with self._write_lock:
            self._get_collection().delete(
                where={"$and": [{"user_id": user_id}, {"md5": md5}]}
            )
        logger.info(f"【向量数据库】已删除用户 {user_id} 中 md5={md5} 的文档")

    def delete_by_user(self, user_id: str):
        """清空用户所有文档，异常透传给上层处理回滚。"""
        with self._write_lock:
            self._get_collection().delete(where={"user_id": user_id})
        logger.info(f"【向量数据库】已删除用户 {user_id} 的所有文档")

    def get_user_documents(self, user_id: str) -> list[dict]:
        """获取用户所有文档的 metadata（仅拉取 metadata，避免传输 embeddings）。"""
        try:
            results = self._get_collection().get(
                where={"user_id": user_id},
                include=["metadatas"],
            )
            return results.get("metadatas", []) if results else []
        except Exception as e:
            logger.error(f"【向量数据库】获取用户文档出错: {e}")
            return []

    def close(self):
        """关闭 ChromaDB 客户端，释放 SQLite 连接。"""
        if self._store is not None:
            try:
                self._store._client.close()
                logger.info("【向量数据库】ChromaDB 连接已关闭")
            except Exception as e:
                logger.error(f"【向量数据库】关闭连接失败: {e}")
            self._store = None
        VectorStoreService._instance = None
