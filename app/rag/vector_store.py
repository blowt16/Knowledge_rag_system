"""ChromaDB 向量存储 — 双重检查锁定单例 + 用户隔离。"""
import threading
import yaml
from app.utils.path_tool import resolve_path, get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _load_chroma_config() -> dict:
    config_path = resolve_path("app/config/chroma.yaml")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class VectorStoreService:
    """ChromaDB 单例管理 — 线程安全双重检查锁定。"""

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._collection = None
                    cls._instance._config = _load_chroma_config()
        return cls._instance

    @property
    def collection_name(self) -> str:
        return self._config.get("collection_name", "rag_collection")

    @property
    def k(self) -> int:
        return self._config.get("k", 3)

    @property
    def persist_directory(self) -> str:
        return str(get_data_path("chromadb"))

    def _get_client(self):
        """获取或创建 ChromaDB 持久化客户端。"""
        import chromadb
        from chromadb.config import Settings

        return chromadb.PersistentClient(
            path=self.persist_directory,
            settings=Settings(anonymized_telemetry=False),
        )

    def get_collection(self):
        """获取或创建 collection（懒加载）。"""
        if self._collection is None:
            try:
                from chromadb.api import SharedSystemClient
                SharedSystemClient.clear_system_cache()
            except ImportError:
                pass

            client = self._get_client()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"【向量数据库】Collection '{self.collection_name}' 已就绪")
        return self._collection

    def add_documents(self, documents: list, embeddings_model=None):
        """批量添加文档到 ChromaDB。

        Args:
            documents: LangChain Document 列表，每个 doc.metadata 需包含 user_id, md5 等
            embeddings_model: Embedding 模型实例（可选，为 None 时使用默认 embedding_function）
        """
        if not documents:
            return
        collection = self.get_collection()

        ids = [f"{doc.metadata.get('md5', 'unknown')}_{i}" for i, doc in enumerate(documents)]
        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]

        if embeddings_model:
            embeddings = embeddings_model.embed_documents(texts)
            collection.add(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)
        else:
            collection.add(ids=ids, documents=texts, metadatas=metadatas)

        logger.info(f"【向量数据库】已入库 {len(documents)} 条文档")

    def similarity_search(self, query: str, user_id: str, k: int = None) -> list:
        """向量相似度检索，按 user_id 隔离。

        Returns:
            list[Document]: LangChain Document 列表
        """
        if k is None:
            k = self.k
        collection = self.get_collection()
        results = collection.query(
            query_texts=[query],
            n_results=k,
            where={"user_id": user_id},
        )
        return self._to_documents(results)

    def delete_by_md5(self, user_id: str, md5: str):
        """按 MD5 删除文档。"""
        collection = self.get_collection()
        try:
            collection.delete(
                where={"$and": [{"user_id": user_id}, {"md5": md5}]}
            )
            logger.info(f"【向量数据库】已删除用户 {user_id} 中 md5={md5} 的文档")
        except Exception as e:
            logger.error(f"【向量数据库】删除出错: {e}")

    def delete_by_user(self, user_id: str):
        """清空用户所有文档。"""
        collection = self.get_collection()
        try:
            collection.delete(where={"user_id": user_id})
            logger.info(f"【向量数据库】已删除用户 {user_id} 的所有文档")
        except Exception as e:
            logger.error(f"【向量数据库】删除出错: {e}")

    def get_user_documents(self, user_id: str) -> list[dict]:
        """获取用户所有文档的 metadata。"""
        collection = self.get_collection()
        try:
            results = collection.get(where={"user_id": user_id})
            return results.get("metadatas", []) if results else []
        except Exception as e:
            logger.error(f"【向量数据库】获取用户文档出错: {e}")
            return []

    def _to_documents(self, results: dict) -> list:
        """将 ChromaDB 查询结果转为 LangChain Document 列表。"""
        from langchain_core.documents import Document

        documents = []
        if not results or not results.get("ids"):
            return documents

        ids_list = results["ids"][0] if results["ids"] else []
        docs_list = results["documents"][0] if results["documents"] else []
        metas_list = results["metadatas"][0] if results["metadatas"] else []
        distances = results.get("distances", [[]])[0] if results.get("distances") else []

        for i, doc_id in enumerate(ids_list):
            doc = Document(
                id=doc_id,
                page_content=docs_list[i] if i < len(docs_list) else "",
                metadata=metas_list[i] if i < len(metas_list) else {},
            )
            if i < len(distances):
                doc.metadata["score"] = 1.0 - distances[i]
            documents.append(doc)

        return documents
