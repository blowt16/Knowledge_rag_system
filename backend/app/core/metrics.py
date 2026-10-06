"""指标（§3.2.3.3）。

分工（别混）：
- **业务指标**（文档数 / 问答量 / 拒答率 / 降级次数…）落 **PostgreSQL**，
  由 `services/stats_service.py` 直查 —— **不在这里**。
- **运行指标**（耗时分位数、错误率、token 用量）走 **OTLP 导出** 到 Collector，
  经 `spanmetrics` 与自定义计数器进 Prometheus，由 `stats/retrieval` 用 PromQL 读。

这里只管第二类，且只做一件当前确有消费方的事：**LLM token 用量**
（§4.4 仪表盘的「LLM token 用量」面板）。

⚠️ 与 `telemetry.setup_tracing` 同一口径：**没配 `OTEL_EXPORTER_OTLP_ENDPOINT`
   就不挂 exporter**。跑测试、跑 CLI 时不往外发（否则每次调用都要试连一个
   不存在的 Collector，刷错误日志、拖慢退出）。
"""

from __future__ import annotations

import logging
import os

from opentelemetry import metrics as otel_metrics

logger = logging.getLogger(__name__)

_meter = None
_llm_tokens = None
_initialized = False


def setup_metrics(service_name: str = "campus-rag") -> None:
    """初始化 MeterProvider。没配端点时只建 meter、不导出。"""
    global _meter, _llm_tokens, _initialized
    if _initialized:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            # 默认 60s。问答是稀疏事件，等一分钟才在 Prometheus 里看见会让人以为没接上
            export_interval_millis=15000,
        )
        provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}),
            metric_readers=[reader],
        )
        otel_metrics.set_meter_provider(provider)
        logger.info("OTLP 指标导出已启用", extra={"event": "metrics.exporter",
                                             "endpoint": endpoint})
    else:
        logger.info("未配置 OTEL_EXPORTER_OTLP_ENDPOINT —— 指标不导出",
                    extra={"event": "metrics.exporter_disabled"})

    _meter = otel_metrics.get_meter("campus-rag")
    _llm_tokens = _meter.create_counter(
        name="llm_tokens_total",
        unit="1",
        description="LLM token 用量，按输入/输出与调用点分组",
    )
    _initialized = True


def record_llm_tokens(prompt_tokens: int, completion_tokens: int,
                      model: str, node: str = "unknown") -> None:
    """记一次 LLM 调用的 token 用量。

    ⚠️ 属性里只有**计数与标签**，没有 prompt / 回答正文（§3.2.3.2）。
    """
    if _llm_tokens is None:
        return
    if prompt_tokens:
        _llm_tokens.add(prompt_tokens, {"llm.token_type": "prompt",
                                        "llm.model": model, "rag.node": node})
    if completion_tokens:
        _llm_tokens.add(completion_tokens, {"llm.token_type": "completion",
                                            "llm.model": model, "rag.node": node})
