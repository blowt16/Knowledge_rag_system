"""模型工厂 — Chat / Embedding / Vision 模型统一创建与切换。"""
import os
import threading


def _get_llm_type() -> str:
    return os.getenv("LLM_TYPE", "DEEPSEEK").upper()


def _get_embed_type() -> str:
    return os.getenv("EMBED_MODEL_TYPE", "ALIYUN").upper()


def _get_vision_type() -> str:
    return os.getenv("VISION_MODEL_TYPE", "ALIYUN").upper()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, ""))
    except ValueError:
        return default


def get_api_key() -> str:
    key = _env("ALIYUN_ACCESS_KEY")
    if key and not os.getenv("DASHSCOPE_API_KEY"):
        os.environ["DASHSCOPE_API_KEY"] = key
    return key


def get_ollama_base_url() -> str:
    return _env("OLLAMA_BASE_URL", "http://localhost:11434")


# ============================================================
# Chat Model
# ============================================================

def create_chat_model():
    """创建 Chat 模型，通过 LLM_TYPE 环境变量切换。"""
    llm_type = _get_llm_type()
    temperature = _env_float("LLM_TEMPERATURE", 0.7)

    if llm_type == "DEEPSEEK":
        from langchain_openai import ChatOpenAI
        from langchain_community.chat_models import ChatTongyi

        deepseek_key = _env("DEEPSEEK_API_KEY")
        if deepseek_key:
            primary = ChatOpenAI(
                model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
                openai_api_key=deepseek_key,
                openai_api_base=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                temperature=temperature,
            )
        else:
            primary = ChatOpenAI(
                model=_env("DEEPSEEK_ALIYUN_MODEL", "deepseek-v4-pro"),
                openai_api_key=get_api_key(),
                openai_api_base=_env("ALIYUN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                temperature=temperature,
            )

        fallback = ChatTongyi(
            model_name=_env("QWEN_MODEL_NAME", "qwen3-max"),
            dashscope_api_key=get_api_key(),
            temperature=temperature,
        )
        return primary.with_fallbacks([fallback])
    elif llm_type == "QWEN":
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(
            model_name=_env("QWEN_MODEL_NAME", "qwen3-max"),
            dashscope_api_key=get_api_key(),
            temperature=temperature,
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

    if embed_type == "ALIYUN":
        from langchain_community.embeddings import DashScopeEmbeddings
        return DashScopeEmbeddings(
            model=_env("ALIYUN_EMBED_MODEL", "text-embedding-v4"),
            dashscope_api_key=get_api_key(),
        )
    elif embed_type == "OLLAMA":
        from langchain_community.embeddings import OllamaEmbeddings
        return OllamaEmbeddings(
            model=_env("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b"),
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
    """创建视觉模型 — 仅支持阿里云百炼多模态。

    使用 ChatOpenAI + DashScope 兼容模式端点，而非 ChatTongyi。
    原因：DashScope 原生多模态 API 不支持 data:image/xxx;base64 格式，
    仅接受 HTTP URL 或本地文件路径。兼容模式端点原生支持 OpenAI 格式的
    base64 data URL，与 HumanMessage 中 image_url 的标准用法一致。
    """
    vision_type = _get_vision_type()

    if vision_type != "ALIYUN":
        raise ValueError(
            f"不支持的 VISION_MODEL_TYPE: {vision_type}，仅支持 ALIYUN。"
            f"Ollama 视觉模型已移除，请使用阿里云百炼多模态。"
        )

    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=_env("ALIYUN_VISION_MODEL", "qwen3.7-max-2026-06-08"),
        openai_api_key=get_api_key(),
        openai_api_base=_env(
            "ALIYUN_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    )
