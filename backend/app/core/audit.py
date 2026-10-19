"""统一审计日志出口。

约定：只记标识符、枚举、变更字段名与计数；**不记密码、正文、token**。
事件名统一为 `audit.<module>.<action>`，控制台用 `stage=audit.<module>` 展示。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import trace

logger = logging.getLogger("app.audit")


def audit(event: str, *, stage: str, ok: bool = True,
          level: int = logging.INFO, **fields: Any) -> None:
    """写一条审计事件。

    `event` 可以带或不带 `audit.` 前缀；`ok=False` 时调用方应同时传
    `level=logging.WARNING`（失败）或 `logging.ERROR`（异常）。
    """
    name = event if event.startswith("audit.") else f"audit.{event}"
    logger.log(level, "审计", extra={
        "event": name,
        "stage": stage,
        "layer": "audit",
        "ok": ok,
        "trace_id": trace.current_trace_id(),
        **fields,
    })
