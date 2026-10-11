"""可观测性统一出口 —— Langfuse（【方案 §5】，取代原 `core/telemetry.py`）。

**只有本模块 import langfuse**，其余模块一律经这里取能力（【方案 决策 4】：
代码不得直接 import opentelemetry，langfuse 同理）。

⚠️ 三条实测踩出来的铁律，改动前先读【方案 §12.1】：

1. **未配置密钥时 `get_current_trace_id()` 返回 `None`**，不是全 0 串。
   直接写进日志会变成 `"trace_id": null`，破坏既有契约
   （前端错误提示、日志检索都按「合法字符串」用它）。
2. **SDK 的缺省 host 是 `https://cloud.langfuse.com`**
   （源码第四顺位：`base_url or LANGFUSE_BASE_URL or host or LANGFUSE_HOST or 默认`）。
   配了 key 漏配地址 = 数据静默发去公有云 → 本项目是隐私事故。
   因此 `resolved_base_url()` **必须**返回配置里写死的值，绝不用 SDK 默认。
3. **没配密钥 = 完全不发外网**——跑测试、跑 CLI 就是这个状态，行为必须与改造前一致。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.core.config import cfg

logger = logging.getLogger(__name__)

#: 无有效 trace 时的合法占位（与改造前 telemetry.py 同语义）
_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16

#: 当前图节点名。⚠️ 不能从 SDK 取——实测 `obs.name` 不存在，
#: 名字只藏在私有属性 `obs._otel_span.name` 里（【方案 §12.1 修正 1】）。
_current_node: ContextVar[str] = ContextVar("rag_node", default="unknown")

_client: Any = None
_enabled = False
_service_name = "campus-rag"


def _reset_for_test() -> None:
    """测试用：清掉单例。**不要在生产代码里调**。"""
    global _client, _enabled
    _client = None
    _enabled = False


def resolved_base_url() -> str | None:
    """解析 Langfuse 地址。

    ⚠️ 不用 SDK 的缺省值。本函数只认配置/环境变量；两者都没有时返回 None，
    此时 `setup()` 会走「不启用」分支（不发外网），而不是落到公有云。
    """
    from_env = os.environ.get("LANGFUSE_BASE_URL", "").strip()
    if from_env:
        return from_env
    configured = (cfg("observability.langfuse_base_url", "") or "").strip()
    return configured or None


def _credentials() -> tuple[str, str]:
    """取 public/secret key；缺任一个都视为「未配置」。"""
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    return pub, sec


def setup(service_name: str = "campus-rag") -> None:
    """初始化客户端。**没配密钥就不启用**（不发外网，行为同改造前）。"""
    global _client, _enabled, _service_name
    if _enabled:
        return
    _service_name = service_name

    pub, sec = _credentials()
    base_url = resolved_base_url()

    if not (pub and sec and base_url):
        logger.info(
            "未配置 Langfuse 密钥/地址 —— 观测数据不外发（跑测试与 CLI 的正常状态）",
            extra={"event": "observability.disabled"},
        )
        return

    from langfuse import Langfuse

    _client = Langfuse(
        public_key=pub,
        secret_key=sec,
        # ★ 显式传参（源码优先级①），绕开环境变量与 SDK 缺省值
        base_url=base_url,
        environment=cfg("observability.environment", "development"),
    )
    _enabled = True
    logger.info("Langfuse 已启用", extra={"event": "observability.enabled",
                                      "base_url": base_url})


def shutdown() -> None:
    """退出时 flush（短生命周期进程不 flush 会丢数据）。"""
    global _client, _enabled
    if _client is not None:
        try:
            _client.flush()
            _client.shutdown()
        except Exception:  # noqa: BLE001 —— 退出流程不能被埋点故障卡住
            logger.warning("Langfuse 关闭失败", extra={"event": "observability.shutdown_failed"})
    _client = None
    _enabled = False


def get_client():
    return _client


def current_trace_id() -> str:
    """32 位十六进制。**无有效 trace 时返回全 0 串，永不为 None。**"""
    if _client is None:
        return _ALL_ZERO_TRACE
    try:
        return _client.get_current_trace_id() or _ALL_ZERO_TRACE
    except Exception:  # noqa: BLE001 —— 日志不能因为埋点故障而挂掉（logging.py:73 同款）
        return _ALL_ZERO_TRACE


def current_span_id() -> str:
    if _client is None:
        return _ALL_ZERO_SPAN
    try:
        return _client.get_current_observation_id() or _ALL_ZERO_SPAN
    except Exception:  # noqa: BLE001
        return _ALL_ZERO_SPAN


@contextmanager
def attach_trace(trace_id: str, span_id: str = "") -> Iterator[None]:
    """把新任务挂回已有 trace（取代原 `telemetry.reattach`）。

    ⚠️ 后台任务/SSE 生成器不能靠 contextvars 隐式继承，必须显式传递
    （【方案 §3.2.3.1】）。本函数用官方 `trace_context` 参数实现，
    实测能把新观测挂成原 trace 的子节点（【方案 §12.1】#2）。
    """
    if _client is None:
        yield
        return

    ctx: dict[str, str] = {"trace_id": trace_id}
    if span_id and span_id != _ALL_ZERO_SPAN:
        ctx["parent_span_id"] = span_id

    with _client.start_as_current_observation(
        as_type="span", name="resumed", trace_context=ctx,
    ):
        yield


def current_node() -> str:
    """当前图节点名（供 `llm.py` 打 tag）。"""
    return _current_node.get()


@contextmanager
def node_context(name: str) -> Iterator[None]:
    """在图节点执行期间记住节点名。见 §12.1 修正 1 为什么必须这么做。"""
    token = _current_node.set(name)
    try:
        yield
    finally:
        _current_node.reset(token)
