"""重排序服务 — CrossEncoder BGE-Reranker-v2-m3。"""
import os
import threading
from app.utils.path_tool import get_models_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class ReorderService:
    """CrossEncoder 重排序服务 — 延迟加载 BGE-Reranker-v2-m3 模型。"""

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
        """延迟加载重排序模型。"""
        if self._model is None:
            model_path = os.getenv("RERANKER_MODEL_PATH", "models/bge-reranker-v2-m3")
            model_dir = get_models_path(model_path)

            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(
                    str(model_dir),
                    max_length=512,
                )
                device = "cuda" if self._model.model.device.type != "cpu" else "cpu"
                logger.info(f"✅ 加载重排序模型: {model_dir}, 使用设备: {device}")
            except Exception as e:
                logger.error(f"❌ 模型检查失败: {e}")
                # 尝试从 ModelScope 下载
                try:
                    from modelscope import snapshot_download
                    model_dir = snapshot_download("BAAI/bge-reranker-v2-m3", cache_dir=str(get_models_path()))
                    from sentence_transformers import CrossEncoder
                    self._model = CrossEncoder(str(model_dir), max_length=512)
                    logger.info(f"✅ 从 ModelScope 下载并加载模型成功")
                except Exception as e2:
                    logger.error(f"❌ ModelScope 下载也失败: {e2}")
                    self._model = None
        return self._model

    def rerank(self, query: str, documents: list, top_k: int = 3) -> list:
        """对文档列表重排序，返回 top_k 个相关性最高的文档。

        Args:
            query: 查询文本
            documents: Document 列表
            top_k: 返回数量

        Returns:
            按相关性分数降序排列的 Document 列表，每个 doc.metadata 含 rerank_score
        """
        if not documents:
            return []

        model = self._get_model()
        if model is None:
            logger.warning("【重排序服务】模型未就绪，返回原始排序")
            return documents[:top_k]

        try:
            pairs = [(query, doc.page_content[:512]) for doc in documents]
            scores = model.predict(pairs, batch_size=1, show_progress_bar=False)

            for doc, score in zip(documents, scores):
                doc.metadata["rerank_score"] = float(score)
                logger.debug(f"【重排序服务】文档相似度分数: {float(score):.4f}")

            ranked = sorted(
                zip(documents, scores),
                key=lambda x: x[1],
                reverse=True,
            )
            result = [doc for doc, _ in ranked[:top_k]]
            logger.info(f"【重排序服务】文档重排序成功，返回 {len(result)} 个文档")
            return result

        except Exception as e:
            logger.error(f"【重排序服务】重排序失败: {e}")
            return documents[:top_k]
