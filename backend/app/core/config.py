"""配置加载：YAML + .env（§3.2.1）。

硬性要求：密钥一律走环境变量，任何地方不得硬编码密钥值。
本模块只提供「按环境变量名取密钥」的入口，配置里存的是变量名。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# backend/app/core/config.py → backend/ → 仓库根
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
CONFIG_FILE = BACKEND_DIR / "app" / "config" / "app.yaml"

# .env 在仓库根（已在 .gitignore 第 151 行）
load_dotenv(REPO_ROOT / ".env")


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """加载并缓存 app.yaml。"""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def cfg(path: str, default: Any = None) -> Any:
    """点分路径取配置值，如 cfg("retrieval.k")。

    路径不存在时返回 default —— 配置项与代码必须严格对应（附录 A 第 3/9 条），
    因此调用方应尽量显式给 default，便于发现拼错。
    """
    node: Any = get_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def require_env(name: str) -> str:
    """取必需的环境变量；缺失时立刻报错，不要静默用空串兜底。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"缺少必需的环境变量 {name}。请检查仓库根的 .env（不得提交进版本库）。"
        )
    return value


def secret(env_key: str) -> str:
    """按键名取密钥 —— 配置里存的是环境变量名，这里才解析成实际值（§3.2.1）。"""
    return require_env(env_key)


# ---- 常用路径 ----------------------------------------------------------

def repo_path(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)


def data_dir() -> Path:
    return repo_path("data")


def pg_dsn() -> str:
    """PostgreSQL 连接串。口令从 .env 取，不写进配置。"""
    host = env("PG_HOST", "127.0.0.1")
    port = env("PG_PORT", "5432")
    db = env("PG_DATABASE", "campus_rag")
    user = require_env("PG_USER")
    password = require_env("PG_PASSWORD")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"
