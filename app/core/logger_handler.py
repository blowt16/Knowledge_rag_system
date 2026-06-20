"""底层日志配置 — Handler / Formatter 管理。"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


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
    ) -> None:
        if cls._configured:
            return

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

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
            root_logger.addHandler(file_handler)

        cls._configured = True
