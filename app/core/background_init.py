"""后台初始化管理器 — 异步加载重型模型，不阻塞 uvicorn 启动。"""
import asyncio
import time
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class _BackgroundInitManager:
    """单例后台初始化管理器。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._models_ready = asyncio.Event()
            cls._instance._chromadb_ready = asyncio.Event()
            cls._instance._reranker_ready = asyncio.Event()
            cls._instance._chat_model = None
            cls._instance._embed_model = None
        return cls._instance

    @property
    def chat_model(self):
        return self._chat_model

    @property
    def embed_model(self):
        return self._embed_model

    @property
    def models_ready(self) -> asyncio.Event:
        return self._models_ready

    @property
    def chromadb_ready(self) -> asyncio.Event:
        return self._chromadb_ready

    @property
    def reranker_ready(self) -> asyncio.Event:
        return self._reranker_ready

    def start(self):
        """在 FastAPI startup 事件中调用，启动后台初始化任务。"""
        if self._initialized:
            return
        self._initialized = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._initialize_all())
        except RuntimeError:
            pass

    async def _initialize_all(self):
        start_time = time.time()
        logger.info("[INIT] 开始后台初始化...")

        try:
            await self._init_models()
            await self._init_chromadb()
            await self._init_reranker()
            elapsed = time.time() - start_time
            logger.info(f"[OK]后台初始化完成，耗时 {elapsed:.1f} 秒")
        except Exception as e:
            logger.error(f"[ERR]后台初始化失败: {e}")

    async def _init_models(self):
        """初始化 Chat / Embedding 模型（Vision 由 VisionService 延迟加载）。"""
        from app.utils.factory import create_chat_model, create_embedding_model

        try:
            self._chat_model = create_chat_model()
            logger.info("[OK]chat_model 初始化完成")
        except Exception as e:
            logger.warning(f"[WARN]chat_model 初始化失败: {e}")

        try:
            self._embed_model = create_embedding_model()
            logger.info("[OK]embed_model 初始化完成")
        except Exception as e:
            logger.warning(f"[WARN]embed_model 初始化失败: {e}")

        self._models_ready.set()

    async def _init_chromadb(self):
        """初始化 ChromaDB 向量数据库（等待 models_ready）。"""
        await self._models_ready.wait()
        try:
            from app.rag.vector_store import VectorStoreService
            VectorStoreService()
            logger.info("[OK]ChromaDB 向量数据库初始化完成")
        except Exception as e:
            logger.error(f"[ERR]ChromaDB 初始化失败: {e}")
        self._chromadb_ready.set()

    async def _init_reranker(self):
        """初始化重排序模型（启动时加载到内存）。"""
        await self._models_ready.wait()
        try:
            from app.rag.reorder_service import ReorderService
            svc = ReorderService()
            await asyncio.to_thread(svc.warmup)
            logger.info("[OK]ReorderService 模型已加载到内存")
        except Exception as e:
            logger.warning(f"[WARN]ReorderService 初始化失败: {e}")
        self._reranker_ready.set()

    def shutdown(self):
        """优雅关闭，释放所有模型和连接资源。"""
        logger.info("[SHUTDOWN] 开始清理资源...")

        # 1. 清理 ReorderService
        try:
            from app.rag.reorder_service import ReorderService
            inst = ReorderService._instance
            if inst is not None:
                inst.close()
        except Exception as e:
            logger.warning(f"[SHUTDOWN] ReorderService 清理失败: {e}")

        # 2. 清理 VectorStoreService
        try:
            from app.rag.vector_store import VectorStoreService
            inst = VectorStoreService._instance
            if inst is not None:
                inst.close()
        except Exception as e:
            logger.warning(f"[SHUTDOWN] VectorStoreService 清理失败: {e}")

        # 3. 清理 ChatOpenAI httpx 连接池（DEEPSEEK 模式下 primary 为 ChatOpenAI）
        try:
            if self._chat_model is not None:
                bound = getattr(self._chat_model, "bound", None)
                if bound is not None:
                    if hasattr(bound, "root_client"):
                        bound.root_client.close()
                    if hasattr(bound, "root_async_client"):
                        bound.root_async_client.close()
        except Exception as e:
            logger.warning(f"[SHUTDOWN] ChatModel 清理失败: {e}")

        # 4. 置空引用
        self._chat_model = None
        self._embed_model = None
        logger.info("[SHUTDOWN] 资源清理完成")


# 全局单例
init_manager = _BackgroundInitManager()
