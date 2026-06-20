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
            cls._instance._vision_model = None
        return cls._instance

    @property
    def chat_model(self):
        return self._chat_model

    @property
    def embed_model(self):
        return self._embed_model

    @property
    def vision_model(self):
        return self._vision_model

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
        logger.info("🔄 开始后台初始化...")

        try:
            await self._init_models()
            await self._init_chromadb()
            await self._init_reranker()
            elapsed = time.time() - start_time
            logger.info(f"✅ 后台初始化完成，耗时 {elapsed:.1f} 秒")
        except Exception as e:
            logger.error(f"❌ 后台初始化失败: {e}")

    async def _init_models(self):
        """初始化 Chat / Embedding / Vision 模型。"""
        from app.utils.factory import create_chat_model, create_embedding_model, create_vision_model

        try:
            self._chat_model = create_chat_model()
            logger.info("✅ chat_model 初始化完成")
        except Exception as e:
            logger.warning(f"⚠️ chat_model 初始化失败: {e}")

        try:
            self._embed_model = create_embedding_model()
            logger.info("✅ embed_model 初始化完成")
        except Exception as e:
            logger.warning(f"⚠️ embed_model 初始化失败: {e}")

        try:
            self._vision_model = create_vision_model()
            logger.info("✅ vision_model 初始化完成")
        except Exception as e:
            logger.warning(f"⚠️ vision_model 初始化失败: {e}")

        self._models_ready.set()

    async def _init_chromadb(self):
        """初始化 ChromaDB 向量数据库（等待 models_ready）。"""
        await self._models_ready.wait()
        try:
            from app.rag.vector_store import VectorStoreService
            VectorStoreService()
            logger.info("✅ ChromaDB 向量数据库初始化完成")
        except Exception as e:
            logger.error(f"❌ ChromaDB 初始化失败: {e}")
        self._chromadb_ready.set()

    async def _init_reranker(self):
        """初始化重排序模型（异步下载 + 加载）。"""
        await self._models_ready.wait()
        try:
            from app.rag.reorder_service import ReorderService
            ReorderService()
            logger.info("✅ ReorderService 初始化完成")
        except Exception as e:
            logger.warning(f"⚠️ ReorderService 初始化失败: {e}")
        self._reranker_ready.set()


# 全局单例
init_manager = _BackgroundInitManager()
