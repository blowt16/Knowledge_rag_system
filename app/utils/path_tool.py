"""路径统一管理 — 以项目根目录为基准，将相对路径解析为绝对路径。"""
from pathlib import Path


def get_project_root() -> Path:
    """返回项目根目录（pyproject.toml 所在目录）。"""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current.parent.parent


def resolve_path(relative_path: str) -> Path:
    """将相对路径解析为绝对路径。"""
    return get_project_root() / relative_path


def get_data_path(subpath: str = "") -> Path:
    """获取 data/ 目录下的绝对路径。"""
    path = resolve_path(f"data/{subpath}") if subpath else resolve_path("data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path(filename: str = "") -> Path:
    """获取 db/ 目录下的绝对路径。"""
    base = resolve_path("db")
    base.mkdir(parents=True, exist_ok=True)
    return base / filename if filename else base


def get_logs_path(subpath: str = "") -> Path:
    """获取 logs/ 目录下的绝对路径。"""
    base = resolve_path("logs")
    base.mkdir(parents=True, exist_ok=True)
    return base / subpath if subpath else base


def get_models_path(model_name: str = "") -> Path:
    """获取 models/ 目录下的绝对路径。"""
    base = resolve_path("models")
    base.mkdir(parents=True, exist_ok=True)
    return base / model_name if model_name else base
