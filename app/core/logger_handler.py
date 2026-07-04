"""底层日志配置 — Handler / Formatter 管理。"""
import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from app.config.loader import get_config


class _WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """Windows 下日志轮转时处理 PermissionError（杀毒软件/索引占用文件）。"""

    def doRollover(self):
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = self.rotation_filename("%s.%d" % (self.baseFilename, i))
                    dfn = self.rotation_filename("%s.%d" % (self.baseFilename, i + 1))
                    if os.path.exists(sfn):
                        try:
                            if os.path.exists(dfn):
                                os.remove(dfn)
                        except PermissionError:
                            pass
                        try:
                            os.rename(sfn, dfn)
                        except PermissionError:
                            pass
                dfn = self.rotation_filename(self.baseFilename + ".1")
                try:
                    if os.path.exists(dfn):
                        os.remove(dfn)
                except PermissionError:
                    pass
                try:
                    self.rotate(self.baseFilename, dfn)
                except PermissionError:
                    try:
                        os.remove(dfn)
                    except OSError:
                        pass
            if not self.delay:
                self.stream = self._open()
        except Exception:
            # 轮转失败（文件被占用等）→ 放弃轮转，不影响日志继续写入
            if not self.delay:
                try:
                    self.stream = self._open()
                except Exception:
                    pass


_DEFAULT_NOISY = ("pdfminer", "watchfiles", "urllib3", "httpcore", "httpx", "PIL")


def _get_noisy_prefixes() -> tuple[str, ...]:
    prefixes = get_config("noisy_logger_prefixes", list(_DEFAULT_NOISY))
    if prefixes:
        return tuple(prefixes)
    return _DEFAULT_NOISY


class _EmbeddingsDebugFilter(logging.Filter):
    """过滤 DEBUG 级别中包含 embedding 关键词的日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG:
            return True
        msg = record.getMessage()
        if isinstance(msg, str) and "embedding" in msg.lower():
            return False
        return True


class _ThirdPartyFilter(logging.Filter):
    """抑制嘈杂第三方库的 WARNING 及以下日志（保留 ERROR/CRITICAL）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # ERROR 和 CRITICAL 始终放行
        if record.levelno >= logging.ERROR:
            return True
        name = record.name
        for prefix in _get_noisy_prefixes():
            if name.startswith(prefix):
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
        log_filename: str = "app.log",
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
        console_handler.addFilter(_ThirdPartyFilter())
        root_logger.addHandler(console_handler)

        # 文件 Handler
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = _WindowsSafeRotatingFileHandler(
                log_dir / log_filename,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
            file_handler.setFormatter(formatter)
            file_handler.addFilter(_EmbeddingsDebugFilter())
            file_handler.addFilter(_ThirdPartyFilter())
            root_logger.addHandler(file_handler)

        cls._configured = True
