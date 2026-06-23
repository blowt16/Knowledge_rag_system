"""知识库 RAG 系统 — FastAPI 主入口。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 自动加载 .env 文件
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)
except ImportError:
    pass

from app.utils.log_tool import setup_logger, get_logger
from app.core.background_init import init_manager
from app.core.failed_response import (
    AppException, DocumentLoadException,
    app_exception_handler, general_exception_handler,
)

# 初始化日志系统
setup_logger()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理。

    uvicorn --reload 模式下子进程会丢失父进程配置的 logging handler，
    因此在 startup 阶段 force 重建 handler，确保日志正常输出。
    """
    setup_logger(force=True)
    logger.info("[START] 服务启动，开始后台初始化...")
    init_manager.start()
    yield
    init_manager.shutdown()


# 创建 FastAPI 应用
app = FastAPI(
    title="知识库 RAG 系统",
    description="本地知识库 RAG 检索系统 — FastAPI + LangChain + ChromaDB",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册异常处理器
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(DocumentLoadException, app_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# 注册路由
from app.router.chat_router import chat_router
from app.router.knowledge_router import knowledge_router
from app.router.conversation_router import conversation_router
from app.router.zip_router import zip_router

app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(conversation_router)
app.include_router(zip_router)


@app.get("/")
async def root():
    return {
        "service": "知识库 RAG 系统",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
