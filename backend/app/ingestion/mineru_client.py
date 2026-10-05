"""MinerU 云端解析客户端 —— 扫描件 / 文字层不可信的页（§E.8.6 / §E.9）。

分工（§E.8.6）：
    我们（按页）  这页**有没有**可信文字层 → 决定**送不送**
    MinerU       这几页**怎么解析** → 它自己的 auto 判定（范围已收窄）
    我们（兜底）  MinerU 返回空 → 记入缺失清单（B.1.2）

⚠️ 与「扫描件沿用 MinerU」不冲突：不是不能用 MinerU，
   是**不能让它对整份文档下判断**。

⚠️ token 过期会返回 `401 {"msgCode":"A0211"}`（不带 token 是 `login required`，
   两种 401 可区分）。撞到 401 先看 msgCode 是不是 A0211（过期），
   别一上来就怀疑服务端故障或代码问题。

⚠️ 服务可用性：2026-10-04 实测 `extract` 返回 `state="done"`，原「卡在 uploading」
   未复现 —— 但**只成功过 1 次**，M0 正式接入时应多跑几份确认。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.config import cfg, require_env

logger = logging.getLogger(__name__)


class MinerUError(RuntimeError):
    pass


class MinerUTokenExpired(MinerUError):
    """token 过期 —— 需要用户在 MinerU 控制台续期/轮换。"""


def _client():
    from mineru import MinerU

    token = require_env(cfg("mineru.token_env", "MINERU_TOKEN"))
    return MinerU(token=token)


def extract_pages(pdf_path: Path, pages: str) -> list[dict[str, Any]]:
    """按页码范围解析，返回内容块列表（每块含 `page_idx`）。

    `pages` 形如 `"15-16"` 或 `"99"`：**1-based、两端闭区间**（附录 F.2 实测）。

    ⚠️ 返回的 `page_idx` 是 **0-based、相对本次请求的子集**，不是绝对页码。
       调用方必须重映射：`绝对页 = 请求起始页 + page_idx`。
    """
    timeout = int(cfg("mineru.timeout", 900))
    try:
        client = _client()
        result = client.extract(
            str(pdf_path),
            language="ch",
            timeout=timeout,
            formula=True,
            table=True,
            pages=pages,
        )
    except Exception as e:  # noqa: BLE001 — 云服务异常一律降级，不让主链路崩
        message = str(e)
        if "A0211" in message or "token expired" in message.lower():
            logger.error("MinerU token 已过期（msgCode A0211）—— 请续期",
                         extra={"event": "mineru.token_expired"})
            raise MinerUTokenExpired(message) from e
        logger.warning("MinerU 解析失败，按缺失页处理：%s", message,
                       extra={"event": "mineru.failed", "node": "ingest"})
        raise MinerUError(message) from e

    state = getattr(result, "state", None)
    if state != "done":
        raise MinerUError(f"MinerU 返回状态异常：{state}")

    content = getattr(result, "content_list", None) or []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        blocks.append({
            "page_idx": item.get("page_idx", 0),
            "text": item.get("text", ""),
            "type": item.get("type", "text"),
        })
    return blocks


def is_available() -> tuple[bool, str]:
    """探活。不实际发起解析（那要花钱与时间），只检查 token 是否存在。"""
    import os

    env_key = cfg("mineru.token_env", "MINERU_TOKEN")
    if not os.getenv(env_key):
        return False, f"未设置 {env_key}"
    return True, "ok"
