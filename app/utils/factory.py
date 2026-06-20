"""模型工厂 — Chat / Embedding / Vision 模型统一创建与切换。"""
import os
import threading
from typing import Optional


def _get_llm_type() -> str:
    return os.getenv("LLM_TYPE", "DEEPSEEK").upper()


def _get_embed_type() -> str:
    return os.getenv("EMBED_MODEL_TYPE", "ALIYUN").upper()


def _get_vision_type() -> str:
    return os.getenv("VISION_MODEL_TYPE", "ALIYUN").upper()


def get_api_key() -> str:
    key = os.getenv("ALIYUN_ACCESS_KEY", "")
    # ChatTongyi 要求 DASHSCOPE_API_KEY 环境变量
    if key and not os.getenv("DASHSCOPE_API_KEY"):
        os.environ["DASHSCOPE_API_KEY"] = key
    return key


def get_ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


# ============================================================
# Chat Model
# ============================================================

def create_chat_model():
    """创建 Chat 模型，通过 LLM_TYPE 环境变量切换。"""
    llm_type = _get_llm_type()
    api_key = get_api_key()

    if llm_type == "DEEPSEEK":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model="deepseek-v4-pro",
            openai_api_key=api_key,
            openai_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.7,
        )
    elif llm_type == "QWEN":
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(
            model_name="qwen3-max",
            dashscope_api_key=api_key,
            temperature=0.7,
        )
    else:
        raise ValueError(f"不支持的 LLM_TYPE: {llm_type}，可选值: DEEPSEEK / QWEN")


# ============================================================
# Embedding Model (含 _LazyEmbedding)
# ============================================================

class _LazyEmbedding:
    """延迟加载 Embedding 模型，首次 embed_documents / embed_query 时才初始化。"""

    def __init__(self):
        self._embedding = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._embedding is None:
            with self._lock:
                if self._embedding is None:
                    self._embedding = _create_embedding()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        return self._embedding.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        self._ensure_loaded()
        return self._embedding.embed_query(text)


def _create_embedding():
    """创建 Embedding 模型，通过 EMBED_MODEL_TYPE 环境变量切换。"""
    embed_type = _get_embed_type()
    api_key = get_api_key()

    if embed_type == "ALIYUN":
        from langchain_community.embeddings import DashScopeEmbeddings
        return DashScopeEmbeddings(
            model="text-embedding-v4",
            dashscope_api_key=api_key,
        )
    elif embed_type == "OLLAMA":
        from langchain_community.embeddings import OllamaEmbeddings
        return OllamaEmbeddings(
            model="qwen3-embedding:0.6b",
            base_url=get_ollama_base_url(),
        )
    else:
        raise ValueError(f"不支持的 EMBED_MODEL_TYPE: {embed_type}，可选值: ALIYUN / OLLAMA")


def create_embedding_model():
    """返回 _LazyEmbedding 包装器，延迟加载。"""
    return _LazyEmbedding()


# ============================================================
# Vision Model
# ============================================================

def create_vision_model():
    """创建视觉模型，通过 VISION_MODEL_TYPE 环境变量切换。"""
    vision_type = _get_vision_type()
    api_key = get_api_key()

    if vision_type == "ALIYUN":
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(
            model_name="qwen3.7-max-2026-06-08",
            dashscope_api_key=api_key,
        )
    elif vision_type == "OLLAMA":
        from langchain_community.chat_models import ChatOllama
        return ChatOllama(
            model="qwen-vl:7b",
            base_url=get_ollama_base_url(),
        )
    else:
        raise ValueError(f"不支持的 VISION_MODEL_TYPE: {vision_type}，可选值: ALIYUN / OLLAMA")
