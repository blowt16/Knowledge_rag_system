"""前端配置模块 — 读取 front_config.yaml（可选），环境变量可覆盖。

优先级: 环境变量(FRONT_前缀) > front_config.yaml > 默认值
如果 pyyaml 未安装，跳过 YAML 文件读取，仅使用环境变量 + 默认值。
"""
import json
import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "front_config.yaml"


def _load_config() -> dict:
    """加载 front_config.yaml，不可用时返回空字典。"""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        import yaml
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return {}
    except Exception:
        return {}


_yaml = _load_config()


def _get(key: str, default):
    """按优先级读取配置: 环境变量(FRONT_前缀) > YAML > default。"""
    env_key = f"FRONT_{key.upper()}"
    env_val = os.getenv(env_key)
    if env_val is not None:
        if isinstance(default, list):
            try:
                return json.loads(env_val)
            except (json.JSONDecodeError, TypeError):
                return [x.strip() for x in env_val.split(",")]
        if isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                pass
        return env_val
    return _yaml.get(key, default)


# ============================================================
# 配置项
# ============================================================
API_BASE_URL = os.getenv("RAG_API_BASE", _get("api_base_url", "http://127.0.0.1:8000"))
USER_ID = _get("default_user_id", "default_user")

# 文件上传
MAX_SINGLE_SIZE = _get("max_single_file_size", 100 * 1024 * 1024)
MAX_ZIP_SIZE = _get("max_zip_file_size", 50 * 1024 * 1024)
ALLOWED_SINGLE = _get("allowed_single_extensions", ["txt", "pdf", "md", "pptx", "docx"])
ALLOWED_ZIP = _get("allowed_zip_extensions", ["zip", "tar", "gz"])

# 分页
PAGE_SIZE = _get("pagination_page_size", 20)

# 聊天
DEFAULT_CHAT_MODE = _get("default_chat_mode", "agent")
