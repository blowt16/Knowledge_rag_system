"""重排序服务 — CrossEncoder BGE-Reranker-v2-m3。"""
import gc
import os
import threading
from pathlib import Path
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
        model_path = os.getenv("RERANKER_MODEL_PATH", "models/BAAI/bge-reranker-v2-m3")
        scope_name = os.getenv("RERANKER_MODELSCOPE_NAME", "BAAI/bge-reranker-v2-m3")

        from sentence_transformers import CrossEncoder

        device = os.getenv("RERANKER_DEVICE", "cuda")

        loaded_path = None

        def _try_load(path):
            nonlocal device, loaded_path
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
            loaded_path = str(path)
            return self._model

        # 1. 本地路径（环境变量指定或默认）
        if self._model is None:
            try:
                _try_load(get_models_path(model_path))
            except Exception:
                pass

        # 2. ModelScope 缓存目录（snapshot_download 下载后存放的位置）
        if self._model is None:
            cache_dir = get_models_path(scope_name)
            if cache_dir.exists():
                try:
                    _try_load(cache_dir)
                except Exception as e:
                    logger.error(f"[ERR] 从缓存目录加载失败: {e}")

        # 3. ModelScope 下载
        if self._model is None:
            try:
                from modelscope import snapshot_download
                model_dir = snapshot_download(scope_name, cache_dir=str(get_models_path()))
                _try_load(model_dir)
            except Exception as e:
                logger.error(f"[ERR] ModelScope 下载失败: {e}")

        # 首次加载后保存 modules.json（消除后续启动的 No modules.json 日志）
        if self._model is not None and loaded_path:
            modules_file = Path(loaded_path) / "modules.json"
            if not modules_file.exists():
                try:
                    self._model.save(str(loaded_path))
                    logger.info(f"[OK] 已保存 modules.json 到 {loaded_path}")
                except Exception as e:
                    logger.debug(f"保存 modules.json 失败(可忽略): {e}")

        return self._model

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
            batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "10"))
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
        """释放模型内存 — 先移到 CPU 再删除，确保 GPU 显存彻底释放。"""
        if self._model is not None:
            try:
                self._model.model.to("cpu")
            except Exception:
                pass
            del self._model
            self._model = None
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            gc.collect()
        ReorderService._instance = None
