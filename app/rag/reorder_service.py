"""重排序服务 — CrossEncoder BGE-Reranker-v2-m3。"""
import os
import threading
from app.config.loader import get_config
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
        if self._model is None:
            model_path = os.getenv("RERANKER_MODEL_PATH", "models/bge-reranker-v2-m3")
            model_dir = get_models_path(model_path)
            max_length = int(os.getenv("RERANKER_MAX_LENGTH", "512"))

            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(str(model_dir), max_length=max_length)
                device = "cuda" if self._model.model.device.type != "cpu" else "cpu"
                logger.info(f"[OK] 加载重排序模型: {model_dir}, 使用设备: {device}")
            except Exception as e:
                logger.error(f"[ERR] 模型检查失败: {e}")
                try:
                    from modelscope import snapshot_download
                    scope_name = os.getenv("RERANKER_MODELSCOPE_NAME", "BAAI/bge-reranker-v2-m3")
                    model_dir = snapshot_download(scope_name, cache_dir=str(get_models_path()))
                    from sentence_transformers import CrossEncoder
                    self._model = CrossEncoder(str(model_dir), max_length=max_length)
                    logger.info("[OK] 从 ModelScope 下载并加载模型成功")
                except Exception as e2:
                    logger.error(f"[ERR] ModelScope 下载也失败: {e2}")
                    self._model = None
        return self._model

    def rerank(self, query: str, documents: list, top_k: int = None) -> list:
        if not documents:
            return []

        if top_k is None:
            top_k = get_config("k", 3)

        model = self._get_model()
        if model is None:
            logger.warning("【重排序服务】模型未就绪，返回原始排序")
            return documents[:top_k]

        try:
            max_len = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
            pairs = [(query, doc.page_content[:max_len]) for doc in documents]
            batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "1"))
            scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)

            for doc, score in zip(documents, scores):
                doc.metadata["rerank_score"] = float(score)
                logger.debug(f"【重排序服务】文档相似度分数: {float(score):.4f}")

            ranked = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
            result = [doc for doc, _ in ranked[:top_k]]
            logger.info(f"【重排序服务】文档重排序成功，返回 {len(result)} 个文档")
            return result
        except Exception as e:
            logger.error(f"【重排序服务】重排序失败: {e}")
            return documents[:top_k]
