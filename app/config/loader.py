"""统一配置加载 — chroma.yaml 单例缓存。"""
import yaml
from pathlib import Path
from app.utils.path_tool import resolve_path

_config_cache: dict | None = None


def load_chroma_config() -> dict:
    """加载 chroma.yaml 配置（缓存）。"""
    global _config_cache
    if _config_cache is None:
        config_path = resolve_path("app/config/chroma.yaml")
        with open(config_path, encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def reload_config():
    """强制重新加载配置（热更新用）。"""
    global _config_cache
    _config_cache = None
    return load_chroma_config()


def get_config(key: str, default=None):
    """按点号分隔的 key 读取配置项。例如 get_config('k', 3)。"""
    cfg = load_chroma_config()
    return cfg.get(key, default)
