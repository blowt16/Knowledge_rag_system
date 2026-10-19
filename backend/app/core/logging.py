"""结构化日志：JSON 文件 + 人读控制台 + trace_id 注入 + 脱敏（§3.2.3.2）。

⚠️ 脱敏是必须项，不是可选项：
    用户提问原文、检索片段、答案正文一律不进日志，只记 query_len 与 query_hash。
    校园场景的提问可能含学号、姓名、成绩等个人信息。
    需要排查时按 trace_id 去 qa_logs 取 —— 那里本来就有全文，且受 ACL 保护。

⚠️ span 属性必须同级脱敏 —— 见 observability.py 的 mask_otel_spans。这里只管住日志。
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Any

from app.core import observability, trace
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

# ---------------------------------------------------------------- 控制台格式

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}

_LEVEL_COLOR = {
    "DEBUG": "dim",
    "INFO": "",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
}

#: 控制台字段白名单 —— 其他字段只进 JSON 文件。
_CONSOLE_FIELDS = (
    "query_len", "query_hash", "session_id", "user_id",
    "route", "route_source", "decision", "changed", "skipped",
    "queries", "candidates", "recalled", "kept", "top", "confidence",
    "degraded", "evidence", "chars", "citations", "reason", "facets",
    "refused", "inputs", "outputs", "rrf_k",
    "model", "node", "messages", "history", "ttft_ms",
    "tokens_in", "tokens_out", "tokens",
    "hits", "folded", "retry",
    "pages", "ocr_pages", "tables", "chunks", "batches", "vectors",
    "before_tokens", "after_tokens", "summary_chars",
    "chars_in", "chars_out", "file_type", "supported", "duplicate",
    "size", "source", "dim", "bm25", "doc_id", "version", "task_id",
    "doc_filename", "chapters", "run_id",
    "case_id", "turn", "hit",
    "status", "method", "path", "error",
)

#: JSON 顶层字段，不能再落进 extra。
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "event", "session_id",
    "user_id", "node", "task_id",
    "trace_id", "span_id", "stage", "depth", "stage_id", "parent_stage_id",
    "layer", "ok",
}


def hash_query(text: str) -> str:
    """提问的短哈希 —— 用于在不留正文的前提下关联同一问题的多次出现。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def query_len(text: str) -> int:
    return len(text or "")


def _scrub(obj: Any) -> Any:
    """递归把敏感键的值换成占位符。"""
    if isinstance(obj, dict):
        return {
            # 敏感键里**数字**是数量（messages=3/history=5），不是正文；
            # 字符串/容器仍然整值打码。
            k: (_REDACTED
                if k.lower() in _SENSITIVE_KEYS
                and not isinstance(v, (int, float, bool))
                else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class _DropLangfuseContextNoise(logging.Filter):
    """Langfuse 在没有 active span 时会刷 "Context error" WARNING。

    本项目的 local trace 已经兜底；这类噪音会把真正的阶段日志淹没。
    只丢这一条，其余 Langfuse 告警照常保留。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "No active span in current context" not in record.getMessage()


_LANGFUSE_CONTEXT_NOISE = _DropLangfuseContextNoise()


def _force_utf8(stream: Any) -> None:
    """Windows 控制台默认 GBK；控制台里的 ✔/✖ 需要 UTF-8，否则写入时抛异常。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 不支持 reconfigure 的流保持原样
        pass


def _color_enabled() -> bool:
    mode = str(cfg("logging.console_color", "auto")).lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001 —— 终端探测失败就当不支持颜色
        return False


def _fmt_ms(ms: Any) -> str:
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return str(ms)
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{int(value)}ms"


def _safe_value(value: Any, max_chars: int) -> str:
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    elif isinstance(value, (list, tuple)):
        text = ",".join(str(v) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


class ConsoleFormatter(logging.Formatter):
    """控制台人读格式：时间 级别 [trace8] 缩进 阶段 状态 消息 耗时 key=value。"""

    def __init__(self, use_color: bool = False) -> None:
        super().__init__()
        self.use_color = use_color
        self.max_value_chars = int(cfg("logging.console_max_value_chars", 120))

    def _color(self, text: str, name: str) -> str:
        if not self.use_color or not name:
            return text
        return f"{_ANSI.get(name, '')}{text}{_ANSI['reset']}"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        try:
            return self._format(record)
        except Exception:  # noqa: BLE001 —— 日志格式器绝不能拖垮业务
            return super().format(record)

    def _format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}"

        trace_id = getattr(record, "trace_id", "") or observability.current_trace_id()
        if trace_id and trace_id.strip("0"):
            short_id = trace_id[:8]
        else:
            short_id = "--------"

        stage = getattr(record, "stage", "") or trace.current_stage()
        if stage.startswith("node."):
            stage = stage[len("node."):]
        stage = stage or "-"

        depth = getattr(record, "depth", None)
        if depth is None:
            depth = trace.current_depth()
        indent = "  " * max(int(depth), 0)

        event = getattr(record, "event", "")
        ok = getattr(record, "ok", None)
        if event == "stage.failed" or record.levelno >= logging.ERROR or ok is False:
            status, status_color = "✖", "red"
        elif event == "stage.start":
            status, status_color = "…", "dim"
        elif ok is True:
            status, status_color = "✔", "green"
        else:
            status, status_color = "·", "dim"

        message = record.getMessage().replace("\r", " ").replace("\n", " ")
        level = f"{record.levelname:<7}"
        line = (
            f"{self._color(ts, 'dim')} "
            f"{self._color(level, _LEVEL_COLOR.get(record.levelname, ''))}"
            f"{self._color(f'[{short_id}]', 'magenta')} "
            f"{indent}{self._color(f'{stage:<12}', 'cyan')} "
            f"{self._color(status, status_color)} {message}"
        )

        ms = getattr(record, "ms", None)
        if ms is None:
            # chat 汇总行带的是 latency_ms / elapsed_ms，不是 span 的 ms
            ms = getattr(record, "latency_ms", None)
        if ms is None:
            ms = getattr(record, "elapsed_ms", None)
        if ms is not None:
            line += f" {self._color(_fmt_ms(ms), 'dim')}"

        fields = []
        for key in _CONSOLE_FIELDS:
            value = getattr(record, key, None)
            if value is None or value == "":
                continue
            fields.append(f"{key}={_safe_value(value, self.max_value_chars)}")
        if fields:
            line += "  " + " ".join(fields)

        return line


class JsonFormatter(logging.Formatter):
    """每条日志一行 JSON。

    必填字段（§3.2.3.2）：ts / level / logger / msg / trace_id / span_id / event
    可选：session_id / user_id / node / task_id / stage / depth / stage_id /
    parent_stage_id
    """

    def format(self, record: logging.LogRecord) -> str:
        # trace_id / span_id 优先取记录上已注入的值（trace_span / HTTP 层），
        # 否则回退到 Langfuse / 本地观测上下文。
        trace_id = (getattr(record, "trace_id", "")
                    or observability.current_trace_id())
        span_id = (getattr(record, "span_id", "")
                   or observability.current_span_id())

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

        stage = getattr(record, "stage", None) or trace.current_stage() or None
        if stage:
            payload["stage"] = stage
        depth = getattr(record, "depth", None)
        if depth is None:
            depth = trace.current_depth()
        payload["depth"] = depth
        stage_id = getattr(record, "stage_id", None) or trace.current_span_id() or None
        if stage_id:
            payload["stage_id"] = stage_id
        parent_stage_id = getattr(record, "parent_stage_id", None) or None
        if parent_stage_id:
            payload["parent_stage_id"] = parent_stage_id

        for key in ("session_id", "user_id", "node", "task_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        # 其余 extra 一并带上，但过一遍脱敏
        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extra:
            payload["extra"] = _scrub(extra)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def install() -> None:
    """安装日志：控制台人读文本 + JSON 文件轮转。"""
    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr)
    level = (env(cfg("logging.level_env", "LOG_LEVEL"), None)
             or cfg("logging.default_level", "INFO")).upper()
    console_level = str(cfg("logging.console_level", level)).upper()
    console_format = str(cfg("logging.console_format", "text")).lower()

    json_formatter = JsonFormatter()
    console_formatter: logging.Formatter
    if console_format == "json":
        console_formatter = json_formatter
    else:
        console_formatter = ConsoleFormatter(use_color=_color_enabled())

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(console_level)
    stream.setFormatter(console_formatter)
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
    file_handler.setFormatter(json_formatter)
    root.addHandler(file_handler)

    # 降噪：这些库的 INFO 太吵
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "chromadb",
                  "jieba", "bm25s"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Langfuse/OTel 在没有 active span 时会打 "Context error" WARNING；
    # 本项目的 local trace 已经兜底，这类噪音只会淹没真正的阶段日志。
    logging.getLogger("opentelemetry.context").setLevel(logging.ERROR)
    logging.getLogger("langfuse").addFilter(_LANGFUSE_CONTEXT_NOISE)

    # uvicorn：启动/错误日志走应用格式器；访问日志由 http 阶段行负责，
    # 关掉默认 access logger，避免每个请求出现两行。
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
