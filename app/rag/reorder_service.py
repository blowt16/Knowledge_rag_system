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

    def warmup(self):
        """预加载模型（启动时调用，避免首次检索卡顿）。"""
        self._get_model()

    def _get_model(self):
        if self._model is not None:
            return self._model

        max_length = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
        model_path = os.getenv("RERANKER_MODEL_PATH", "bge-reranker-v2-m3")
        scope_name = os.getenv("RERANKER_MODELSCOPE_NAME", "BAAI/bge-reranker-v2-m3")

        from sentence_transformers import CrossEncoder

        device = os.getenv("RERANKER_DEVICE", "cuda")

        def _try_load(path):
            nonlocal device
            try:
                self._model = CrossEncoder(str(path), max_length=max_length, device=device)
            except (RuntimeError, MemoryError) as e:
                if device != "cpu":
                    logger.warning(f"[WARN] GPU 加载失败({e})，回退到 CPU")
                    device = "cpu"
                    self._model = CrossEncoder(str(path), max_length=max_length, device="cpu")
                else:
                    raise
            try:
                actual_device = str(self._model.model.device)
            except Exception:
                actual_device = device
            logger.info(f"[OK] 加载重排序模型: {path}, 设备: {actual_device}")
            return self._model

        # 1. 本地路径（环境变量指定或默认）
        try:
            return _try_load(get_models_path(model_path))
        except Exception:
            pass

        # 2. ModelScope 缓存目录（snapshot_download 下载后存放的位置）
        cache_dir = get_models_path(scope_name)
        if cache_dir.exists():
            try:
                return _try_load(cache_dir)
            except Exception as e:
                logger.error(f"[ERR] 从缓存目录加载失败: {e}")

        # 3. ModelScope 下载
        try:
            from modelscope import snapshot_download
            model_dir = snapshot_download(scope_name, cache_dir=str(get_models_path()))
            return _try_load(model_dir)
        except Exception as e:
            logger.error(f"[ERR] ModelScope 下载失败: {e}")
            self._model = None
            return None

    def rerank(self, query: str, documents: list, top_k: int = None) -> list:
        if not documents:
            return []

        if top_k is None:
            top_k = get_config("k", 5)

        model = self._get_model()
        if model is None:
            logger.warning("【重排序服务】模型未就绪，返回原始排序")
            return documents[:top_k]

        try:
            try:
                device_type = str(model.model.device)
            except Exception:
                device_type = "unknown"
            max_len = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
            pairs = [(query, doc.page_content[:max_len]) for doc in documents]
            batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "1"))
            scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)

            for doc, score in zip(documents, scores):
                doc.metadata["rerank_score"] = float(score)
                logger.debug(f"【重排序服务】文档相似度分数: {float(score):.4f}")

            ranked = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
            result = [doc for doc, _ in ranked[:top_k]]
            logger.info(f"【重排序服务】重排序完成 [{device_type}]: {len(documents)} → {len(result)} 文档")
            return result
        except Exception as e:
            logger.error(f"【重排序服务】重排序失败: {e}")
            return documents[:top_k]

    def close(self):
        """释放模型内存。"""
        if self._model is not None:
            self._model = None
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
        ReorderService._instance = None
