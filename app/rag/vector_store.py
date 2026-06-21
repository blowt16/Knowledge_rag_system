"""ChromaDB 向量存储 — 基于 langchain_chroma.Chroma + 双重检查锁定单例。"""
import threading
from langchain_chroma import Chroma
from app.config.loader import get_config
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class VectorStoreService:
    """ChromaDB 单例管理。"""

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
        return get_config("k", 3)

    @property
    def persist_directory(self) -> str:
        return str(get_data_path("chromadb"))

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
        """批量添加文档（串行化写入，避免 SQLite 并发写冲突）。"""
        if not documents:
            return
        with self._write_lock:
            ids = [f"{doc.metadata.get('md5', 'unknown')}_{i}" for i, doc in enumerate(documents)]
            self.get_store().add_documents(documents, ids=ids)
            logger.debug(f"【向量数据库】已入库 {len(documents)} 条文档")

    def similarity_search(self, query: str, user_id: str, k: int = None) -> list:
        """向量相似度检索，按 user_id 隔离。"""
        if k is None:
            k = self.k
        return self.get_store().similarity_search(
            query, k=k, filter={"user_id": user_id},
        )

    def delete_by_md5(self, user_id: str, md5: str):
        """按 MD5 删除文档。"""
        with self._write_lock:
            try:
                self._get_collection().delete(
                    where={"$and": [{"user_id": user_id}, {"md5": md5}]}
                )
                logger.info(f"【向量数据库】已删除用户 {user_id} 中 md5={md5} 的文档")
            except Exception as e:
                logger.error(f"【向量数据库】删除出错: {e}")

    def delete_by_user(self, user_id: str):
        """清空用户所有文档。"""
        with self._write_lock:
            try:
                self._get_collection().delete(where={"user_id": user_id})
                logger.info(f"【向量数据库】已删除用户 {user_id} 的所有文档")
            except Exception as e:
                logger.error(f"【向量数据库】删除出错: {e}")

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
