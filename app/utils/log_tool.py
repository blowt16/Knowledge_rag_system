"""日志统一管理 — 所有模块通过此模块获取日志器。"""
import logging
import os
from app.utils.path_tool import get_logs_path
from app.core.logger_handler import LogHandler

_log_level: str = os.getenv("LOG_LEVEL", "INFO")
_setup_done: bool = False


def setup_logger(level: str | None = None) -> None:
    """初始化全局日志系统（仅 main.py 启动时调用一次）。"""
    global _log_level, _setup_done
    if _setup_done:
        return
    if level is not None:
        _log_level = level.upper()
    LogHandler.setup(
        console_level=_log_level,
        file_level="DEBUG",
        log_dir=get_logs_path(),
    )
    _setup_done = True


def get_logger(name: str) -> logging.Logger:
    """获取指定命名空间的日志器。建议传入 __name__。"""
    return logging.getLogger(name)


def get_all_loggers() -> dict[str, logging.Logger]:
    """获取所有已注册的日志器（用于调试）。"""
    return logging.Logger.manager.loggerDict  # type: ignore[return-value]
