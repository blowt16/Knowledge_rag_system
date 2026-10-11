"""可观测性统一出口 —— Langfuse（【方案 §5】，取代原 `telemetry.py`）。

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
import re
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

#: 重入护栏。⚠️ 见 `current_trace_id()` 的说明，别删。
#: **trace_id 与 span_id 共用同一个**：SDK 两条取 id 的路径各自都会 warning，
#: 用两个独立的 ContextVar 就等于留了两条递归支路（实测扇出翻倍，39 次）。
#: 共用还保证与 formatter 里取字段的先后顺序无关。
_in_trace_id: ContextVar[bool] = ContextVar("in_trace_id", default=False)

_client: Any = None
_enabled = False
_service_name = "campus-rag"


def _reset_for_test() -> None:
    """测试用：清掉单例与脱敏规则缓存。**不要在生产代码里调**。"""
    global _client, _enabled, _patterns_cache
    _client = None
    _enabled = False
    _patterns_cache = None


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
        mask_otel_spans=mask_otel_spans,
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
    """32 位十六进制。**无有效 trace 时返回全 0 串，永不为 None。**

    ⚠️⚠️ **重入护栏不能删**（Task 2 实施时实测发现，方案原稿未覆盖）：
        启用客户端 + 无 active span 时，SDK 内部会 warning；
        而 `logging.py` 的 `JsonFormatter.format()` **正好会调本函数** ——
        若 warning 又走到 root handler，就形成
            format() → current_trace_id() → SDK warning → root handler → format() → …
        实测：一行应用日志引发 8 层嵌套重入，峰值 51；
        若同时取 span_id 则扇出翻倍（实测 39 次调用）。
        护栏在第二层直接返回全 0，递归当场切断。
    """
    if _client is None or _in_trace_id.get():
        return _ALL_ZERO_TRACE
    token = _in_trace_id.set(True)
    try:
        return _client.get_current_trace_id() or _ALL_ZERO_TRACE
    except Exception:  # noqa: BLE001 —— 日志不能因为埋点故障而挂掉（logging.py:73 同款）
        return _ALL_ZERO_TRACE
    finally:
        _in_trace_id.reset(token)


def current_span_id() -> str:
    """与 `current_trace_id()` 同构，共用同一个重入护栏（理由见上）。"""
    if _client is None or _in_trace_id.get():
        return _ALL_ZERO_SPAN
    token = _in_trace_id.set(True)
    try:
        return _client.get_current_observation_id() or _ALL_ZERO_SPAN
    except Exception:  # noqa: BLE001
        return _ALL_ZERO_SPAN
    finally:
        _in_trace_id.reset(token)


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


# ---------------------------------------------------------------- 脱敏
# 【方案 §7】：正文照传（保住 Langfuse 的价值），标识符在**导出前**打码。
# 官方钩子 `mask_otel_spans` 在导出阶段跑，即「数据离开应用之前」。

_REDACTED = "[REDACTED:{name}]"
_patterns_cache: list[tuple[str, Any]] | None = None


def _compiled_patterns() -> list[tuple[str, Any]]:
    """编译脱敏正则（带缓存）。配置为空时返回空列表 —— 不脱敏会由测试兜住。"""
    global _patterns_cache
    if _patterns_cache is not None:
        return _patterns_cache
    import re

    raw = cfg("observability.masking.patterns", []) or []
    compiled = []
    for item in raw:
        try:
            compiled.append((item["name"], re.compile(item["regex"])))
        except Exception:  # noqa: BLE001 —— 配置写错不该让服务起不来
            logger.warning("脱敏规则编译失败，已跳过",
                           extra={"event": "observability.mask_rule_invalid",
                                  "rule": item.get("name")})
    _patterns_cache = compiled
    return compiled


def _redact_text(text: str) -> str:
    for name, rx in _compiled_patterns():
        text = rx.sub(_REDACTED.format(name=name), text)
    return text


def _redact_value(value: Any) -> Any:
    """递归脱敏：字符串走正则，容器递归下去，其余原样。"""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def mask_otel_spans(*, params: Any):
    """`mask_otel_spans` 钩子：导出前把 span 属性里的标识符打码。

    ⚠️ 实测确认本钩子**确实在导出前跑**、且打码后的内容才是云端收到的
    （【方案 §12.1】#3，用本地捕获服务器验的）。
    但**别据此认为它永远对** —— `test_langfuse_masking.py` 才是每次都跑的凭据。
    """
    if not cfg("observability.masking.enabled", True):
        return None

    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in (params.spans or {}).items():
        sets = {}
        for key, value in (span.attributes or {}).items():
            redacted = _redact_value(value)
            if redacted != value:
                sets[key] = redacted
        if sets:
            patches[identifier] = OtelSpanPatch(set_attributes=sets)
    return MaskOtelSpansResult(span_patches=patches) if patches else None
