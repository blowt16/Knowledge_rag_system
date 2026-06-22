"""底层日志配置 — Handler / Formatter 管理。"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


class _EmbeddingsDebugFilter(logging.Filter):
    """过滤 DEBUG 级别中包含 embedding 关键词的日志（dashscope 响应等）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG:
            return True
        msg = record.getMessage()
        if isinstance(msg, str) and "embedding" in msg.lower():
            return False
        return True


class LogHandler:
    """日志处理器管理：控制台 StreamHandler + 文件 RotatingFileHandler。"""

    _configured: bool = False

    @classmethod
    def setup(
        cls,
        console_level: str = "INFO",
        file_level: str = "DEBUG",
        log_dir: Path | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        force: bool = False,
    ) -> None:
        if cls._configured and not force:
            return

        if force:
            root = logging.getLogger()
            root.handlers.clear()
            cls._configured = False

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addFilter(_EmbeddingsDebugFilter())

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 控制台 Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # 文件 Handler
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
            file_handler.setFormatter(formatter)
            file_handler.addFilter(_EmbeddingsDebugFilter())
            root_logger.addHandler(file_handler)

        cls._configured = True
