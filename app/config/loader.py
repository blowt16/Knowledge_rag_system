"""统一配置加载 — chroma.yaml + agent.yaml 单例缓存。"""
import yaml
from pathlib import Path
from app.utils.path_tool import resolve_path

_config_cache: dict | None = None


def _load_all_configs() -> dict:
    """加载并合并所有配置文件。"""
    config = {}
    config_dir = resolve_path("app/config")
    for filename in ("chroma.yaml", "agent.yaml"):
        filepath = config_dir / filename
        if filepath.exists():
            with open(filepath, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                config.update(data)
    return config


def load_chroma_config() -> dict:
    """加载全部配置（缓存）。"""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_all_configs()
    return _config_cache


def reload_config():
    """强制重新加载配置（热更新用）。"""
    global _config_cache
    _config_cache = None
    return load_chroma_config()


def get_config(key: str, default=None):
    """按 key 读取配置项。例如 get_config('k', 5)。"""
    cfg = load_chroma_config()
    return cfg.get(key, default)
