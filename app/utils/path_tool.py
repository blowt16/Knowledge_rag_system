"""路径统一管理 — 以项目根目录为基准，将相对路径解析为绝对路径。
所有目录名从 chroma.yaml 读取，可通过 data_path/db_path/logs_path/models_path 配置。"""

from pathlib import Path


def _cfg_dir(key: str, default: str) -> str:
    """读取目录配置，延迟导入避免循环依赖。"""
    try:
        from app.config.loader import get_config
        return get_config(key, default)
    except Exception:
        return default


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
    """获取 data/ 目录下的绝对路径，目录名从 chroma.yaml data_path 读取。"""
    data_dir = _cfg_dir("data_path", "data")
    path = resolve_path(f"{data_dir}/{subpath}") if subpath else resolve_path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path(filename: str = "") -> Path:
    """获取 db/ 目录下的绝对路径，目录名从 chroma.yaml db_path 读取。"""
    db_dir = _cfg_dir("db_path", "db")
    base = resolve_path(db_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / filename if filename else base


def get_logs_path(subpath: str = "") -> Path:
    """获取 logs/ 目录下的绝对路径，目录名从 chroma.yaml logs_path 读取。"""
    logs_dir = _cfg_dir("logs_path", "logs")
    base = resolve_path(logs_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / subpath if subpath else base


def get_models_path(model_name: str = "") -> Path:
    """获取 models/ 目录下的绝对路径，目录名从 chroma.yaml models_path 读取。"""
    models_dir = _cfg_dir("models_path", "models")
    base = resolve_path(models_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / model_name if model_name else base
