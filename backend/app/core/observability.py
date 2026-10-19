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

import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.core import trace
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
    local = trace.current_trace_id()
    if local:
        return local
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
        return trace.current_span_id() or _ALL_ZERO_SPAN
    token = _in_trace_id.set(True)
    try:
        return (_client.get_current_observation_id()
                or trace.current_span_id() or _ALL_ZERO_SPAN)
    except Exception:  # noqa: BLE001
        return trace.current_span_id() or _ALL_ZERO_SPAN
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
#
# ⚠️⚠️ **匹配必须跑在「解码后的正文」上，不能在转义文本上跑**（A7 验收实测的
#     隐私事故，2026-10-11）：SDK 序列化属性值时分两种形态 ——
#       · **字符串原样**（`input="…"`）：中文是字面量，正则直接跑没问题；
#       · **容器走 `json.dumps(..., ensure_ascii=True)`**（`input=[{...}]`、
#         `metadata`）：中文变 `\uXXXX`，于是数字规则 `(?<!\d)` 的「前一个字符」
#         成了**转义末位的十六进制字符** —— 是数字就静默卡掉打码：
#         `是`=`\\u662f`（末位 f）能过，`号`=`\\u53f7`（末位 7）整条规则失效，
#         而「学号2024001234」恰恰是最自然的写法。22 个常见前导字里 16 个如此。
#     转义文本上匹配还会**跨转义边界**啃掉十六进制位：`\d{15}` 曾把
#     `请拨打13800138000` 里的 `打`（`\\u6253`）连着电话号码一起替换掉，
#     正文被改坏成 `请拨\u[REDACTED:id_card]`。
#     故 `_redact_attr_value()` 先解码、再打码、最后按 SDK 同款参数编码回去。

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
    """纯文本脱敏：规则直接跑在字面文本上（调用方保证已解码）。"""
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


def _looks_like_json(text: str) -> bool:
    """SDK 的容器序列化只产生这几种开头（`{` / `[` / `"`）。

    ⚠️ **不能**「`json.loads` 成功就当 JSON」：裸数字 `2024001234` 也是合法
       JSON（int），那样会走容器分支、**跳过按字符串打码**，学号反而直发出去。
    """
    return text.strip()[:1] in ("{", "[", '"')


def _redact_attr_value(value: Any) -> Any:
    """属性值级脱敏入口：字符串先按 SDK 的序列化形态**解一层**再打码。

    | 属性值形态 | 处理 |
    |---|---|
    | 容器序列化（`[{…}]`、`{…}`、`"…"`） | 解码 → 递归打码 → **同款参数**编码回去 |
    | 纯文本 | 直接跑正则（中文是字面量，`(?<!\\d)` 语义本来就对） |
    | 非字符串（bool/int/bytes…） | 原样返回（与改造前一致） |

    ⚠️ 只在**真有命中**时才编码回去 —— 没命中就返回原文，避免把宿主 JSON
       的形状无谓地改一遍（编码参数若与 SDK 不一致就是「另一种损坏」）。
    """
    if not isinstance(value, str):
        return _redact_value(value)
    if not _looks_like_json(value):
        return _redact_text(value)
    try:
        parsed = json.loads(value)
    except ValueError:
        # 只是「看起来像」—— 退回纯文本路径（如用户正文以引号开头）
        return _redact_text(value)
    redacted = _redact_value(parsed)
    if redacted == parsed:
        return value
    # 与 SDK 的 `json.dumps(obj, cls=EventSerializer)` 同款默认参数
    # （`ensure_ascii=True` + 默认分隔符）—— 形状必须与原来一模一样
    return json.dumps(redacted, ensure_ascii=True)


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
            redacted = _redact_attr_value(value)
            if redacted != value:
                sets[key] = redacted
        if sets:
            patches[identifier] = OtelSpanPatch(set_attributes=sets)
    return MaskOtelSpansResult(span_patches=patches) if patches else None
