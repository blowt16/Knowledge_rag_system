"""结构化日志：JSON + trace_id 注入 + 脱敏（§3.2.3.2）。

⚠️ 脱敏是必须项，不是可选项：
    用户提问原文、检索片段、答案正文一律不进日志，只记 query_len 与 query_hash。
    校园场景的提问可能含学号、姓名、成绩等个人信息。
    需要排查时按 trace_id 去 qa_logs 取 —— 那里本来就有全文，且受 ACL 保护。

⚠️ span 属性必须同级脱敏 —— 见 telemetry.py 的说明。这里只管住日志。
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import sys
from typing import Any

from app.core.config import cfg, env, repo_path

# 这些字段名一旦出现在日志的 extra 里，一律替换成占位符。
# 纵深防御：调用方本就不该传，万一手滑也不会把正文写进日志。
_SENSITIVE_KEYS = {
    "query", "question", "answer", "content", "snippet", "text",
    "prompt", "messages", "history", "context", "resolved_query",
    "password", "token", "access_token", "refresh_token", "authorization",
    "api_key", "secret", "mineru_token", "jwt",
}

_REDACTED = "[REDACTED]"


def hash_query(text: str) -> str:
    """提问的短哈希 —— 用于在不留正文的前提下关联同一问题的多次出现。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def query_len(text: str) -> int:
    return len(text or "")


def _scrub(obj: Any) -> Any:
    """递归把敏感键的值换成占位符。"""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if k.lower() in _SENSITIVE_KEYS else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    """每条日志一行 JSON。

    必填字段（§3.2.3.2）：ts / level / logger / msg / trace_id / span_id / event
    可选：session_id / user_id / node / task_id
    """

    def format(self, record: logging.LogRecord) -> str:
        # trace_id / span_id 从 OTel 当前 span 取（没有则为全 0，格式仍合法）
        trace_id = "0" * 32
        span_id = "0" * 16
        try:
            from opentelemetry import trace as otel_trace

            ctx = otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                trace_id = format(ctx.trace_id, "032x")
                span_id = format(ctx.span_id, "016x")
        except Exception:  # noqa: BLE001 — 日志本身不能因为埋点故障而挂掉
            pass

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id,
            "span_id": span_id,
            # 结构化事件名，替代在 msg 里拼字符串
            "event": getattr(record, "event", None),
        }

        for key in ("session_id", "user_id", "node", "task_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        # 其余 extra 一并带上，但过一遍脱敏
        reserved = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "event", "session_id",
            "user_id", "node", "task_id",
        }
        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in reserved and not k.startswith("_")
        }
        if extra:
            payload["extra"] = _scrub(extra)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def install() -> None:
    """安装 JSON 日志。沿用 RotatingFileHandler 轮转，不引入日志检索平台。"""
    level = (env(cfg("logging.level_env", "LOG_LEVEL"), None)
             or cfg("logging.default_level", "INFO")).upper()
    formatter = JsonFormatter()

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # 文件轮转：目录可能不存在（如刚清过 data/），建出来
    log_dir = repo_path(cfg("logging.dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.jsonl",
        maxBytes=int(cfg("logging.max_bytes", 10 * 1024 * 1024)),
        backupCount=int(cfg("logging.backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 降噪：这些库的 INFO 太吵
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "chromadb", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
