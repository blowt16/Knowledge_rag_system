"""意图识别模块配置常量与默认值。"""
from app.config.loader import get_config

# 模型配置
INTENT_MODEL = get_config("INTENT_MODEL_NAME", "deepseek-chat")
INTENT_MODEL_TYPE = get_config("INTENT_MODEL_TYPE", "DEEPSEEK")
INTENT_FALLBACK_MODEL = get_config("INTENT_FALLBACK_MODEL", "qwen3-turbo")

# 超时与降级
INTENT_TIMEOUT = int(get_config("intent_timeout", 5))
INTENT_CACHE_TTL = int(get_config("intent_cache_ttl", 300))
INTENT_CONFIDENCE_THRESHOLD = float(get_config("intent_confidence_threshold", 0.7))

# 历史配置
INTENT_HISTORY_TURNS = int(get_config("intent_history_turns", 2))
INTENT_MAX_HISTORY_CHARS = int(get_config("intent_max_history_chars", 500))
INTENT_MAX_QUERY_CHARS = int(get_config("intent_max_query_chars", 500))

# 总开关
INTENT_ENABLED = get_config("intent_enabled", True)
