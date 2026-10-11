# 可观测性改造（OTel 自建栈 → Langfuse 云）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把可观测性从自建 OTel 三件套换成 Langfuse 云平台——Python SDK 手工埋点、trace_id 全换 Langfuse 的、仪表盘改读 Metrics API v2、告警改后端自算、删掉三个容器与三个配置文件。

**Architecture:** 四个既有收口点（`builder.traced` / `core/llm.py` 三函数 / `main.py` 中间件 / 两处 `reattach`）内部换实现，新增 `core/observability.py` 作统一出口（其余模块不直接 import langfuse）。日志仍走本地 JSON 文件、业务指标仍在 PostgreSQL，两者不动。

**Tech Stack:** Python 3.13 / FastAPI / LangGraph / langfuse 4.17.0（建在 OTel 之上）/ React 19 + Vite

**Spec:** `docs/superpowers/specs/2026-10-11-langfuse-observability-design.md`（850 行，含两轮实测记录）——**本计划不复制该文档的论证，只给「改哪个文件、写什么代码、怎么验」。凡标 `【方案 X.Y】` 处，施工时以方案原文为准。**

---

## Global Constraints

以下每条都是**实测确认的硬约束**，每个任务都隐含包含本节：

| # | 约束 | 出处 |
|---|---|---|
| 1 | Python `>=3.13`；`langfuse>=4.17.0` | `pyproject.toml` / PyPI 实测 |
| 2 | **代码不得直接 `import opentelemetry.*`** —— 全部经由 `core/observability.py` | 【方案 决策 4 / §9.2】 |
| 3 | **所有 Langfuse HTTP 调用必须走 v2 接口** —— v1 对本组织全部 410 | 【方案 §12.4】实测 |
| 4 | `LANGFUSE_BASE_URL` **必须显式配置** —— 缺省值是 `https://cloud.langfuse.com` | 【方案 §10.2】源码实测 |
| 5 | **脱敏是必须项**：提问原文/检索片段/答案正文中的学号等标识符，**导出前**必须打码 | 【方案 §7】 |
| 6 | 未配置密钥时**不得发任何外网请求**，且 `current_trace_id()` 返回 `"0"*32`（**不是 `None`**） | 【方案 §12.1 修正 2】实测 |
| 7 | 后端单 worker（`--workers 1`）—— Chroma 内嵌 | `main.py` 模块说明 |
| 8 | 业务指标（PG 侧）一律不动：`qa_logs` / `degradation_events` / `stats/overview` 等 | 【方案 决策 11】 |
| 9 | 日志仍走本地 `RotatingFileHandler`，不接 Langfuse | 【方案 决策 12】 |
| 10 | 测试命令一律**在仓库根**跑：`.venv/Scripts/python.exe -m pytest ...` | `pyproject.toml` 的 `--basetemp` 是相对路径 |

## Review Focus

以下是**方案暗含但未必有测试覆盖**、且最容易咬到真实使用者的五类情况。每类都已在下方对应任务里配了测试：

| # | 失败模式 | 合理预期 | 落在哪个任务 |
|---|---|---|---|
| 1 | **跑测试/CLI 时没配 Langfuse 密钥** | 日志里 `trace_id` 是合法 32 位串（全 0），**不是 `null`**；不产生任何外网请求 | Task 2 |
| 2 | **配了密钥但漏配 `LANGFUSE_BASE_URL`** | 数据**不得**发去公有云 | Task 2 |
| 3 | **流式 LLM 调用中途抛异常** | generation 仍被 `.end()`，观测**不静默消失** | Task 6 |
| 4 | **Langfuse 云端超时/不可达** | 提问主链路不受影响；仪表盘运行指标区显示「暂不可用」而业务指标照常 | Task 10 / Task 14 |
| 5 | **请求返回 5xx** | 错误率面板读得到该请求（靠 `level=ERROR`） | Task 7 |

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/core/observability.py` | **新建** | 统一出口：客户端单例、trace_id 取值、脱敏钩子、节点/生成观测 |
| `backend/app/core/telemetry.py` | **删除** | 能力并入 observability |
| `backend/app/core/metrics.py` | **删除** | `llm_tokens_total` 由 generation 的 usage 取代 |
| `backend/app/core/logging.py` | 改 | `JsonFormatter` 的 trace_id 换源 |
| `backend/app/graph/builder.py` | 改 | `traced()` 内部实现 |
| `backend/app/core/llm.py` | 改 | 三函数包 generation；`_current_node()` 改读 contextvar |
| `backend/app/main.py` | 改 | 中间件换实现 + 5xx 标 `level=ERROR` |
| `backend/app/api/chat.py` | 改 | `reattach` → `attach_trace` |
| `backend/app/services/document_service.py` | 改 | 同上 |
| `backend/app/services/stats_service.py` | 改 | PromQL → Metrics API v2；告警自算 |
| `backend/app/schemas/stats.py` | 改 | 新增成本 / token 拆分 / 按节点耗时字段 |
| `backend/app/config/app.yaml` | 改 | 新增 `observability` 段（阈值、脱敏规则） |
| `backend/app/config/security.yaml` | 改 | 新增 `langfuse_*` 三个环境变量名 |
| `frontend/web/src/api/schema.d.ts` | 改 | 生成的类型（跟随后端） |
| `frontend/web/src/views/admin/AdminDashboard.tsx` | 改 | 新增指标面板、Jaeger 提示改 Langfuse 深链 |
| `docker-compose.yml` | 改 | 删三个 service + `promdata` |
| `scripts/start.sh` | 改 | 删起容器段落 |
| `otel-collector-config.yaml` / `prometheus.yml` / `prometheus-rules.yml` | **删除** | — |
| `backend/tests/lf_capture.py` | **新建** | 本地捕获服务器（测试用，冒充云端） |
| `backend/tests/unit/test_langfuse_*.py` | **新建** | 6 个新测试文件 |

---

## Task 1: 依赖升级与回归基线

**为什么先做**：Langfuse 要求 `opentelemetry-sdk>=1.45.0`，而当前是 1.42.1（chromadb 带的）。升级可能影响 chromadb，**必须先有基线才知道是不是自己弄坏的**。

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: 无
- Produces: 安装好的 `langfuse` 包；一份「改动前全绿」的基线记录

- [ ] **Step 1: 跑全量测试，记录基线**

```bash
cd /d/Knowledge_rag_system
.venv/Scripts/python.exe -m pytest -q 2>&1 | tail -20
```

**把结果记下来**（通过数 / 失败数 / 失败的是哪些）。若本来就红的，本计划不负责修——但要知道它红。

⚠️ 跑之前先确认后端没在跑（Chroma 是嵌入式的，见 `scripts/start.sh` 开头那条警告）。

- [ ] **Step 2: 记录当前 OTel 版本**

```bash
.venv/Scripts/python.exe -c "import importlib.metadata as md; print('otel-sdk', md.version('opentelemetry-sdk'))"
```

Expected: `otel-sdk 1.42.1`

- [ ] **Step 3: 加依赖**

```bash
cd /d/Knowledge_rag_system
uv add "langfuse>=4.17.0"
```

- [ ] **Step 4: 确认 OTel 被升上去了**

```bash
.venv/Scripts/python.exe -c "import importlib.metadata as md; print('otel-sdk', md.version('opentelemetry-sdk'))"
```

Expected: `1.45.0` 或更高（**若仍是 1.42.1，说明 langfuse 没被真正装上，停下来查**）

- [ ] **Step 5: 重跑全量测试，对比基线**

```bash
.venv/Scripts/python.exe -m pytest -q 2>&1 | tail -20
```

Expected: 与 Step 1 结果**一致**（同样的通过数）。

⚠️ 若出现**新的**失败，尤其是 chromadb 相关的 —— 那是 OTel 升级引起的，**停下来报告，不要继续**。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "[deps] 加 langfuse（连带 OTel 1.42.1 → >=1.45.0），全量测试无回归"
```

---

## Task 2: `core/observability.py` 骨架与 trace_id 兜底

**Files:**
- Create: `backend/app/core/observability.py`
- Create: `backend/tests/unit/test_langfuse_client.py`
- Modify: `backend/app/config/security.yaml`

**Interfaces:**
- Consumes: Task 1 装好的 `langfuse` 包
- Produces（后续任务全部依赖这些签名）：
  - `setup(service_name: str) -> None`
  - `shutdown() -> None`
  - `current_trace_id() -> str`（**永远**返回 32 位十六进制，无有效 trace 时全 0）
  - `current_span_id() -> str`（16 位）
  - `attach_trace(trace_id: str, span_id: str = "") -> ContextManager[None]`
  - `get_client()` —— 返回 Langfuse 客户端或 `None`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_langfuse_client.py`：

```python
"""Langfuse 客户端底座（【方案 §12.1 修正 2/3】）。

两条都是实测踩出来的：
  1. 未配置密钥时 `get_current_trace_id()` 返回 None 而不是全 0 串
     —— 不兜底会让日志字段变成 `"trace_id": null`，破坏既有契约。
  2. SDK 的 host 缺省值是 https://cloud.langfuse.com
     —— 配了 key 漏配地址，数据会静默发去公有云。
"""
from __future__ import annotations

import os

import pytest

from app.core import observability


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「没有 Langfuse 配置」的干净状态开始。"""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    yield
    observability._reset_for_test()


def test_current_trace_id_is_all_zero_without_credentials():
    """没配密钥 → 全 0 串，**不是 None**（这是日志契约的底线）。"""
    observability.setup("campus-rag")
    tid = observability.current_trace_id()
    assert isinstance(tid, str)
    assert tid == "0" * 32


def test_setup_without_credentials_does_not_raise():
    """跑测试/CLI 时没密钥是正常状态，不许抛异常。"""
    observability.setup("campus-rag")      # 不抛即通过


def test_missing_base_url_disables_instead_of_falling_back(monkeypatch):
    """★ 关键：配了 key 但**没配地址**时，宁可不启用，也**不得**落到公有云默认值。

    SDK 源码的解析顺序是
        base_url or LANGFUSE_BASE_URL or host or LANGFUSE_HOST or "https://cloud.langfuse.com"
    —— 落在最后一档就是把数据发去公有云。「不启用」才是安全的那条路。
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
    # 故意不设 LANGFUSE_BASE_URL

    observability.setup("campus-rag")

    assert observability.resolved_base_url() is None
    assert observability.get_client() is None, (
        "没配地址时必须走「不启用」分支 —— 否则 SDK 会用缺省的公有云地址"
    )


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.edu")
    observability._reset_for_test()
    assert observability.resolved_base_url() == "https://lf.example.edu"


def test_span_id_is_all_zero_without_credentials():
    observability.setup("campus-rag")
    assert observability.current_span_id() == "0" * 16
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /d/Knowledge_rag_system
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.observability'`

- [ ] **Step 3: 在 `security.yaml` 加环境变量名**

在 `backend/app/config/security.yaml` 末尾追加：

```yaml

observability:
  # Langfuse 云（【方案 §10.3】）——本文件只存变量名，值在仓库根 .env
  langfuse_public_key: LANGFUSE_PUBLIC_KEY
  langfuse_secret_key: LANGFUSE_SECRET_KEY
  langfuse_base_url: LANGFUSE_BASE_URL
```

- [ ] **Step 4: 写实现**

创建 `backend/app/core/observability.py`：

```python
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
```

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_client.py -v
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/observability.py backend/tests/unit/test_langfuse_client.py backend/app/config/security.yaml
git commit -m "[feat] 可观测性统一出口：Langfuse 客户端 + trace_id 兜底 + 拒绝公有云默认地址"
```

---

## Task 3: 脱敏钩子（`mask_otel_spans`）

**Files:**
- Create: `backend/tests/lf_capture.py`
- Create: `backend/tests/unit/test_langfuse_masking.py`
- Modify: `backend/app/core/observability.py`
- Modify: `backend/app/config/app.yaml`

**Interfaces:**
- Consumes: Task 2 的 `observability.setup()`
- Produces: `observability.mask_otel_spans(**kwargs)` —— 传给 `Langfuse(mask_otel_spans=...)` 的钩子

- [ ] **Step 1: 写本地捕获服务器（测试基础设施）**

创建 `backend/tests/lf_capture.py`：

```python
"""本地捕获服务器：冒充 Langfuse 云端，断言**实际收到的字节**。

⚠️ 为什么不用 mock：mock 掉的正是最容易出问题的接缝
   （上一版 `builder.py:93-97` 就栽在「注释声称安全 ≠ 实际安全」）。

用法：
    with CapturingLangfuse() as cap:
        ...做点什么...
    assert "2023123456" not in cap.body()
"""
from __future__ import annotations

import http.server
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.server.captured.append((self.path, self.rfile.read(n)))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # 静音
        pass


class CapturingLangfuse:
    """起一个本地端口假装是 Langfuse，把收到的请求体攒起来。"""

    def __init__(self, port: int = 0):
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._srv.captured = []          # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "CapturingLangfuse":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()

    def body(self) -> str:
        """所有捕获到的请求体拼成一个字符串。"""
        return "".join(b.decode("utf-8", errors="replace")
                       for _path, b in self._srv.captured)  # type: ignore[attr-defined]

    def clear(self) -> None:
        self._srv.captured.clear()  # type: ignore[attr-defined]
```

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/unit/test_langfuse_masking.py`：

```python
"""脱敏护栏（【方案 §7 / §12.1】#3）。

**实测确认的问题**：日志那边脱敏做了，但 span 是另一个存储。
Langfuse 默认会记录 input/output —— 后果是「日志干净了，云端却摆着学号姓名」。

本测试用**本地捕获服务器**断言实际发出的字节，不是断言 SDK 的自我声明。
"""
from __future__ import annotations

import pytest

from app.core import observability
from tests.lf_capture import CapturingLangfuse

#: 必须被打码的测试学号（10 位纯数字）
STUDENT_ID = "2023123456"
#: 手机号
PHONE = "13800138000"


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_student_id_is_masked_before_export(capture):
    """★ 核心断言：学号不得出现在发出的字节里，打码标记必须出现。"""
    client = observability.get_client()
    assert client is not None

    with client.start_as_current_observation(
        as_type="generation", name="llm-call", model="deepseek-flash",
    ) as gen:
        gen.update(
            input=[{"role": "user", "content": f"学号 {STUDENT_ID} 的缓考申请"}],
            output=f"已受理（学号 {STUDENT_ID}）",
            usage_details={"input": 10, "output": 5},
        )
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert STUDENT_ID not in body, f"学号原样发出去了！收到的内容：\n{body[:500]}"
    assert "[REDACTED:student_id]" in body, "打码标记没出现"


def test_phone_is_masked(capture):
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"联系方式 {PHONE}")
    client.flush()

    body = capture.body()
    assert PHONE not in body, "手机号原样发出去了"
    assert "[REDACTED:phone]" in body


def test_non_sensitive_text_survives(capture):
    """正控：脱敏不能把正常正文也抹掉（否则上面两条断言可能只是因为什么都没发）。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input="图书馆开放时间是什么时候")
    client.flush()

    assert "图书馆开放时间" in capture.body(), "正常正文被误删了"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_masking.py -v
```

Expected: FAIL — 学号原样出现在捕获内容里

- [ ] **Step 4: 在 `app.yaml` 加脱敏规则**

在 `backend/app/config/app.yaml` 末尾追加：

```yaml

observability:
  # 环境名（Langfuse 用来区分 development / production）
  environment: development
  # Langfuse 地址。⚠️ 必须显式配，SDK 缺省值是公有云（【方案 §10.2】）
  #    实际值走 security.yaml 的 langfuse_base_url → 环境变量
  langfuse_base_url: ""
  # 脱敏规则（【方案 §7.3】）——**必须配置化**，不写死在代码里：
  #   校园场景的标识符会变（学号位数、新增证件类型），
  #   写死意味着每次都要改代码 + 重新部署 + 重跑测试。
  masking:
    enabled: true
    patterns:
      - name: student_id          # 学号：10 位纯数字
        regex: '\\b\\d{10}\\b'
      - name: phone               # 手机号
        regex: '\\b1[3-9]\\d{9}\\b'
      - name: id_card             # 身份证
        regex: '\\b\\d{17}[\\dXx]\\b'
      - name: email
        regex: '\\b[\\w.+-]+@[\\w-]+\\.[\\w.]+\\b'
```

- [ ] **Step 5: 实现脱敏钩子**

在 `backend/app/core/observability.py` 的 import 区加 `import re`，并在文件末尾追加：

```python
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
```

同时更新 `_reset_for_test()`，把脱敏缓存也清掉（否则跨用例串味）：

```python
def _reset_for_test() -> None:
    """测试用：清掉单例与脱敏规则缓存。**不要在生产代码里调**。"""
    global _client, _enabled, _patterns_cache
    _client = None
    _enabled = False
    _patterns_cache = None
```

并在 `setup()` 里把钩子接上 —— 将 `_client = Langfuse(...)` 那段改为：

```python
    _client = Langfuse(
        public_key=pub,
        secret_key=sec,
        # ★ 显式传参（源码优先级①），绕开环境变量与 SDK 缺省值
        base_url=base_url,
        environment=cfg("observability.environment", "development"),
        mask_otel_spans=mask_otel_spans,
    )
```

- [ ] **Step 6: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_masking.py -v
```

Expected: 3 passed

> ⚠️ 若 `test_non_sensitive_text_survives` 失败，说明正则太宽，把正常中文也吃了 —— 修正则，不要删测试。

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/observability.py backend/app/config/app.yaml backend/tests/lf_capture.py backend/tests/unit/test_langfuse_masking.py
git commit -m "[feat] 脱敏钩子：学号/手机号/身份证在导出前打码，配置化规则"
```

---

## Task 4: 日志 trace_id 换源

> ⚠️ **本任务与 Task 2 之间有一条已实测的暗礁**（Task 2 实施时发现）：
> 启用客户端 + 无 active span 时，Langfuse SDK 内部会 warning；
> 而本任务让 `JsonFormatter.format()` 调 `current_trace_id()` ——
> 若那条 warning 又走回 root handler，就形成
> `format() → current_trace_id() → SDK warning → root handler → format() → …`
> 实测一行应用日志引发 **8 层嵌套重入，峰值 51**。
>
> **Task 2 已在 `observability.current_trace_id()` / `current_span_id()` 里加了重入护栏**
> （**两个 getter 共用同一个 ContextVar** —— 各自独立会留下第二条递归支路，扇出翻倍）。
> 实测：护栏前 39 次调用，护栏后 3 次（1 条应用日志 + 2 条 SDK warning，
> 两个 getter 各触发一条）—— **warning 仍然照常打出来，诊断信息没丢**。
> **本任务不要再自己加 `propagate = False` 之类的补丁** —— 那会把 warning 一起吞掉。
> 换源完成后，跑一次 `test_langfuse_client.py::test_enabling_client_does_not_recurse_through_logging`
> 确认护栏仍然生效。

**Files:**
- Modify: `backend/app/core/logging.py:62-74`
- Create: `backend/tests/unit/test_logging_trace_id.py`

**Interfaces:**
- Consumes: Task 2 的 `observability.current_trace_id()` / `current_span_id()`（**已带重入护栏**）
- Produces: 日志 JSON 的 `trace_id` 字段恒为 32 位十六进制

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_logging_trace_id.py`：

```python
"""日志的 trace_id 契约（【方案 §6】）。

⚠️ 改动前后都必须成立：**无有效 trace 时是全 0 串，不是 null**。
前端错误提示、按 trace_id 检索日志都依赖它是合法字符串。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core import observability
from app.core.logging import JsonFormatter


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    yield
    observability._reset_for_test()


def _format(msg: str, **extra) -> dict:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return json.loads(JsonFormatter().format(record))


def test_trace_id_is_legal_string_without_langfuse():
    """★ 未配置密钥时也要是合法的 32 位十六进制串，**不能是 null**。"""
    observability.setup("campus-rag")
    payload = _format("hello")

    assert payload["trace_id"] is not None
    assert isinstance(payload["trace_id"], str)
    assert payload["trace_id"] == "0" * 32
    assert payload["span_id"] == "0" * 16


def test_trace_id_is_32_hex_when_enabled(monkeypatch):
    """启用后必须是 32 位十六进制（不是 UUID、不带横线）。"""
    from tests.lf_capture import CapturingLangfuse
    import re

    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")

        client = observability.get_client()
        with client.start_as_current_observation(as_type="span", name="x"):
            payload = _format("hello")
        observability.shutdown()

    assert re.fullmatch(r"[0-9a-f]{32}", payload["trace_id"]), payload["trace_id"]
    assert payload["trace_id"] != "0" * 32


def test_extra_fields_still_scrubbed():
    """回归：改造不能碰坏既有的 extra 脱敏。"""
    payload = _format("q", query="学号2023123456", event="test")
    assert payload["event"] == "test"
    assert "2023123456" not in json.dumps(payload, ensure_ascii=False)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_logging_trace_id.py -v
```

Expected: `test_trace_id_is_32_hex_when_enabled` FAIL（现在读的是 OTel 的当前 span，Langfuse 起了也读不到）；其余可能已通过

- [ ] **Step 3: 改 `logging.py`**

把 `backend/app/core/logging.py` 的 `format()` 开头这段（L62-74）：

```python
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
```

替换为：

```python
        # trace_id / span_id 从 Langfuse 当前观测取
        # （【方案 决策 4】trace_id 全面改用 Langfuse 的，不再碰 OTel）
        # ⚠️ `current_trace_id()` 内部已做 try/except 与 None 兜底 ——
        #    「日志本身不能因为埋点故障而挂掉」这条约束在 observability 里兑现
        trace_id = observability.current_trace_id()
        span_id = observability.current_span_id()
```

并在文件顶部 import 区加：

```python
from app.core import observability
```

> ⚠️ 检查该文件里还有没有别处引用 `otel`（用 `grep -n otel backend/app/core/logging.py` 确认）。有就一并换掉。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_logging_trace_id.py backend/tests/integration/test_purge_logging.py -v
```

Expected: 全 passed（`test_purge_logging` 是回归）

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/logging.py backend/tests/unit/test_logging_trace_id.py
git commit -m "[refactor] 日志 trace_id 换源：OTel 当前 span → Langfuse 当前观测"
```

---

## Task 5: 图节点埋点（`builder.traced`）

**Files:**
- Modify: `backend/app/graph/builder.py:83-128`
- Create: `backend/tests/unit/test_langfuse_node_span.py`
- Delete: `backend/tests/unit/test_span_redaction.py`（由新测试取代）

**Interfaces:**
- Consumes: Task 2 的 `observability.node_context()` / `observability.current_node()`
- Produces: `traced(node_name, fn)` —— **签名与调用点不变**

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_langfuse_node_span.py`：

```python
"""节点 span 的脱敏与生命周期（【方案 §5.1 / §12.1】#5#6）。

**核心承诺**：节点观测里**一个字的正文都没有** —— 节点的输入输出全是
用户提问与检索片段，一旦进 span，日志那边脱敏做得再好也没用。

⚠️ 上一版栽在「注释声称安全 ≠ 实际安全」（builder.py:93-97）。
这条测试就是那份承诺的凭据 —— 人工去云端翻是一次性的，测试是每次都跑的。
"""
from __future__ import annotations

import pytest

from app.core import observability
from app.graph.builder import traced
from tests.lf_capture import CapturingLangfuse

MARKER = "2023123456的缓考申请_正文标记"


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_node_span_carries_no_content(capture):
    """节点的输入/输出都不许出现在发出的字节里。"""

    async def _fake_node(state):
        return {"answer": MARKER, "context": MARKER,
                "trace": [{"node": "generate", "ms": 12, "recalled": 3,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("generate", _fake_node)({"query": MARKER, "session_id": "s1"}))
    observability.get_client().flush()

    body = capture.body()
    assert body, "没发出任何东西 —— 断言会假通过"
    assert MARKER not in body, f"节点正文进了 span：\n{body[:500]}"
    assert "2023123456" not in body, "学号进了 span"


def test_node_span_records_metadata(capture):
    """正控：该记的确实记了（否则上一条可能只是因为什么都没记）。"""

    async def _fake_node(state):
        return {"trace": [{"node": "retrieve", "ms": 42, "recalled": 7,
                           "degraded": None}]}

    import asyncio
    asyncio.run(traced("retrieve", _fake_node)({}))
    observability.get_client().flush()

    body = capture.body()
    assert "node.retrieve" in body, "节点名没进 span"
    # ⚠️ 断言键名而不只是数字 —— 单断言 `"42" in body` 会命中 trace id / 时间戳，
    #    那是「其实什么都没记」也能通过的假绿。
    assert "rag.duration_ms" in body, "耗时字段键名没进 span"
    assert "rag.recalled" in body, "召回数字段键名没进 span"


def test_current_node_is_visible_inside_node(capture):
    """★ §12.1 修正 1：节点执行期间 `current_node()` 必须能读到节点名。

    SDK 不提供「当前观测名」（实测 obs.name 不存在），所以靠 contextvar。
    """
    seen = {}

    async def _fake_node(state):
        seen["node"] = observability.current_node()
        return {}

    import asyncio
    asyncio.run(traced("rewrite", _fake_node)({}))
    assert seen["node"] == "rewrite"


def test_current_node_resets_after_node(capture):
    """节点跑完要还原，不能让上一个节点的名字漏到下一个。"""
    import asyncio

    async def _nop(state):
        return {}

    asyncio.run(traced("route", _nop)({}))
    assert observability.current_node() == "unknown"


def test_node_exception_records_type_only(capture):
    """异常只记类型 —— message 可能回显 prompt（builder.py:90 的既有约定）。"""

    async def _boom(state):
        raise ValueError(f"上游报错，回显内容是：{MARKER}")

    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(traced("rewrite", _boom)({}))
    observability.get_client().flush()

    body = capture.body()
    assert "ValueError" in body, "异常类型没记"
    assert MARKER not in body, "异常 message 带上了正文"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_node_span.py -v
```

Expected: FAIL — 现在的 `traced` 用的是 OTel tracer，不往 Langfuse 发东西 → `body` 为空

- [ ] **Step 3: 改 `builder.py`**

把 `backend/app/graph/builder.py` 的 `traced()`（L83-128）整体替换为：

```python
def traced(node_name: str, fn):
    """给节点套一层 Langfuse 观测（【方案 §5.1】）。

    ⚠️⚠️ **属性里只放"多少钱、几秒、降没降级"，一个字的正文都不放**（【方案 §7】）。
       节点的输入输出全是用户提问与检索片段 —— 一旦当属性写进去，
       日志那边脱敏做得再好也没用，云端照样摆着学号姓名。

    ⚠️⚠️ **必须 `capture_input=False, capture_output=False`**：
       Langfuse 默认会把输入输出与异常都记下来（官方：automatically capture
       inputs, outputs, timings, and errors）。不关掉就等于把 state 整个传上去。

    ⚠️ 异常**只记类型、不记 message**：message 可能回显 prompt。
       实测过「注释声称安全 ≠ 实际安全」的教训（旧 builder.py:93-97），
       `test_langfuse_node_span.py` 是这条承诺每次都跑的凭据。
    """
    @functools.wraps(fn)
    async def _wrapper(state: RAGState) -> dict:
        client = observability.get_client()

        with observability.node_context(node_name):
            if client is None:
                # 未启用（跑测试/CLI）：照常执行，不产生任何观测
                return await fn(state)

            with client.start_as_current_observation(
                as_type="span", name=f"node.{node_name}",
                input=None, output=None,
            ) as obs:
                try:
                    result = await fn(state)
                except Exception as e:  # noqa: BLE001
                    obs.update(level="ERROR",
                               status_message=type(e).__name__,
                               metadata={"rag.error": type(e).__name__})
                    raise

                if isinstance(result, dict):
                    entries = result.get("trace") or []
                    if entries:
                        last = entries[-1]
                        meta = {
                            "rag.duration_ms": int(last.get("ms", 0)),
                            "rag.recalled": int(last.get("recalled", 0)),
                        }
                        degraded = last.get("degraded")
                        if degraded:
                            meta["rag.degraded"] = str(degraded)
                        obs.update(metadata=meta)
                return result

    return _wrapper
```

并改文件顶部的 import：

- `from app.core import telemetry` → `from app.core import observability`
- **删掉** `from opentelemetry.trace import Status, StatusCode`（L42）—— 新实现不再用它。
  ⚠️ 不删就违反 Global Constraints #2，且 T9 之后它会成为全仓唯一残留的 OTel import。

改完用 `grep -n "opentelemetry\|telemetry" backend/app/graph/builder.py` 确认**两样都没了**。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_node_span.py -v
```

Expected: 5 passed

- [ ] **Step 5: 删掉被取代的旧测试**

```bash
git rm backend/tests/unit/test_span_redaction.py
```

> ⚠️ 删之前确认新测试**已经覆盖**它的三条断言：正文不进属性（✓ `test_node_span_carries_no_content`）、
> 属性键白名单（✓ 改为「正文不出现在字节里」，语义等价且更严）、异常只记类型（✓ `test_node_exception_records_type_only`）。

- [ ] **Step 6: 跑图相关回归**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/integration/test_graph_trace.py backend/tests/unit/test_langfuse_node_span.py -v
```

Expected: 全 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/graph/builder.py backend/tests/unit/test_langfuse_node_span.py
git commit -m "[refactor] 图节点埋点换 Langfuse：关内容捕获 + contextvar 记节点名"
```

---

## Task 6: LLM 调用包 generation

> ⚠️ **先读这条（Task 5 实测发现的同类陷阱）**：Langfuse 内部用 OTel 的
> `start_as_current_span`，其 `use_span` 默认 `record_exception=True`，
> 且 SDK **不暴露关闭它的参数** —— 异常一旦穿过 `start_as_current_observation()`
> 的 with 块，`exception.message`（= `str(e)`，可能回显 prompt）与整个栈都会上传。
>
> **本任务用的是 `start_observation()`（手工观测），它不设置当前 OTel 上下文**
> （官方说明：Manual observations 的 "No Context Shift"），因此**理论上不该中招** ——
> 但**这是推断，不是实测**。按「先实测再下结论」：
>
> **Step 1 之前先补一个验证**：写一个临时用例，让 `complete()` 抛异常，
> 断言捕获到的字节里**没有**异常 message。若真的没有 → 手工观测路径安全，照常做下去；
> **若有** → 按 Task 5 的做法处理（在 try 内吞掉、`finally` 里 `obs.end()` 之后再到函数外重抛），
> 并把发现写进报告。**不许跳过这一步直接假设它安全。**

**Files:**
- Modify: `backend/app/core/llm.py`
- Create: `backend/tests/unit/test_langfuse_generation.py`

**Interfaces:**
- Consumes: Task 2 的 `observability.get_client()` / `current_node()`
- Produces: 三个 LLM 函数的签名**不变**；每次调用产生一条 generation 观测（带 usage）

> 附带修一个 Task 5 报告指出的既有小问题：`llm._current_node()` 在**没有 Langfuse 客户端**时
> 会退化成 `"unknown"`。本任务把它改成 `observability.current_node()`（读 contextvar）后，
> 该退化消失 —— 顺手在测试里钉住「未启用客户端时也能取到正确节点名」。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_langfuse_generation.py`：

```python
"""LLM generation 观测（【方案 §5.2】）。

**两条实测踩出来的**：
  - `start_observation()` 忘 `.end()` → 观测**静默消失**，且不报错。
    流式调用（开始与结束不连续）最容易漏，所以专门测它。
  - 本项目 LLM 客户端是**手写 httpx**，Langfuse 的自动集成抓不到 usage，
    必须把 `llm.py` 里本来就在解析的 usage 手工写进观测。
"""
from __future__ import annotations

import pytest

from app.core import llm, observability
from tests.lf_capture import CapturingLangfuse


class _FakeStreamResponse:
    """假 httpx 流：喂一段 SSE。"""

    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_non_stream_records_generation_with_usage(capture, monkeypatch):
    """非流式：观测里要有 model 与 token 数。"""
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "答案"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio
    out = asyncio.run(llm.complete([{"role": "user", "content": "问"}]))
    assert out == "答案"
    observability.get_client().flush()

    body = capture.body()
    assert "deepseek-flash" in body, "model 没进观测"
    # ⚠️ 断言 usage 结构而不只是数字 —— `"11" in body` 会命中 trace id / 时间戳
    assert "usage_details" in body or "usageDetails" in body, "usage 结构没进观测"
    assert "input" in body and "output" in body, "输入/输出没分开记"


def test_stream_generation_is_ended_even_on_error(capture, monkeypatch):
    """★ 核心断言：流式调用**中途抛异常**也必须 `.end()`，观测不能消失。"""
    import httpx

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, *a, **k):
            class _Ctx:
                async def __aenter__(self_inner):
                    async def _boom():
                        yield 'data: {"choices":[{"delta":{"content":"半"}}]}'
                        raise RuntimeError("上游断了")
                    return _FakeStreamResponse(_boom())
                async def __aexit__(self_inner, *a): return False
            return _Ctx()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio

    async def _drain():
        async for _ in llm.stream_raw([{"role": "user", "content": "问"}]):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_drain())

    observability.get_client().flush()
    body = capture.body()
    assert "deepseek-flash" in body, (
        "流式观测没出现 —— 多半是异常路径漏了 .end()（实测：忘 end 会静默丢，不报错）"
    )


def test_generation_records_node_name(capture, monkeypatch):
    """`rag.node` 标签要从 contextvar 取（不能再读 OTel span 名）。"""
    import httpx

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "x"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    import asyncio
    from app.graph.builder import traced

    async def _node(state):
        return {"answer": await llm.complete([{"role": "user", "content": "q"}])}

    asyncio.run(traced("generate", _node)({}))
    observability.get_client().flush()

    # ⚠️ 断言 `rag.node` 标签键，而不是裸的 "generate" ——
    #    节点 span 本身就叫 `node.generate`，裸断言不加这段代码也照样过。
    body = capture.body()
    assert "rag.node" in body, "rag.node 标签没进 generation"
    assert "node.generate" in body, "节点 span 缺失"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_generation.py -v
```

Expected: FAIL — 还没包 generation，capture 收不到东西

- [ ] **Step 3: 改 `llm.py`**

把 `backend/app/core/llm.py` 的 `_current_node()`（L60-68）替换为：

```python
def _current_node() -> str:
    """当前所处的图节点名，用作 generation 的 `rag.node` 标签。

    ⚠️ 改造前是「读 OTel 当前 span 名反推」，但实测 Langfuse **不提供当前观测名**
        （`obs.name` 不存在，【方案 §12.1 修正 1】），故改读 observability 的 contextvar。
    """
    return observability.current_node()
```

把 `_record_usage()`（L71-80）替换为：

```python
def _usage_details(usage: dict[str, Any] | None) -> dict[str, int]:
    """把 API 的 usage 转成 Langfuse 的 usage_details（无 usage 时给空 dict）。"""
    if not usage:
        return {}
    return {
        "input": int(usage.get("prompt_tokens") or 0),
        "output": int(usage.get("completion_tokens") or 0),
    }
```

`complete()` 里把 `_record_usage(data.get("usage"))`（L116）前后改成用观测包裹。
**关键改法**：在函数体开头起观测，`finally` 里 end。把 `complete()` 改为：

```python
async def complete(
    messages: list[dict[str, str]],
    *,
    timeout: float = 30.0,
    max_tokens: int | None = None,
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    """非流式补全，返回 content 文本。"""
    body: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "max_tokens": max_tokens or int(cfg("llm.max_output_tokens_runtime", 8192)),
        "temperature": (temperature if temperature is not None
                        else float(cfg("llm.temperature", 0.3))),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    _apply_thinking(body)

    client = observability.get_client()
    obs = None
    if client is not None:
        obs = client.start_observation(
            as_type="generation", name=f"llm.{_current_node()}",
            model=_model(), input=messages,
            metadata={"rag.node": _current_node()},
        )
    try:
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                r = await http.post(_base() + "/chat/completions",
                                    headers=_headers(), json=body)
        except httpx.TimeoutException as e:
            raise LLMTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise LLMError(str(e)) from e

        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        usage = _usage_details(data.get("usage"))
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构异常：{str(data)[:200]}") from e
        if obs is not None:
            obs.update(output=content, usage_details=usage or None)
        return content
    except Exception as e:  # noqa: BLE001
        if obs is not None:
            obs.update(level="ERROR", status_message=type(e).__name__)
        raise
    finally:
        # ★ 忘 .end() 观测会**静默消失**（实测）—— 必须放 finally
        if obs is not None:
            obs.end()
```

`stream_raw()` 同样处理 —— 在 `try` 之前起观测，`finally` 里 `.end()`，并在收到最后一个带 usage 的 chunk 时 `obs.update(usage_details=...)`：

```python
    client = observability.get_client()
    obs = None
    if client is not None:
        obs = client.start_observation(
            as_type="generation", name=f"llm.{_current_node()}",
            model=_model(), input=messages,
            metadata={"rag.node": _current_node()},
        )
    collected: list[str] = []
    try:
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                async with http.stream("POST", _base() + "/chat/completions",
                                       headers=_headers(), json=body) as response:
                    if response.status_code != 200:
                        detail = (await response.aread())[:200]
                        raise LLMError(f"HTTP {response.status_code}: {detail!r}")
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        # ★ 先取 usage 再判 choices：带 usage 的那个 chunk 的
                        #   choices 是空数组，放到下面就被 continue 掉了
                        usage = _usage_details(chunk.get("usage"))
                        if usage and obs is not None:
                            obs.update(usage_details=usage)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            collected.append(piece)
                            yield piece
        except httpx.TimeoutException as e:
            raise LLMTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise LLMError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        if obs is not None:
            obs.update(level="ERROR", status_message=type(e).__name__)
        raise
    finally:
        # ★ 流式最容易漏 end —— 实测漏了就静默丢观测
        if obs is not None:
            obs.update(output="".join(collected))
            obs.end()
```

并把顶部 import 改为：

```python
from app.core import observability
```

（删掉 `from opentelemetry import trace as otel_trace` 与 `from app.core.metrics import record_llm_tokens`）

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_generation.py -v
```

Expected: 3 passed

- [ ] **Step 5: 跑 LLM 相关回归**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_stream_json_parser.py backend/tests/unit/test_prompts.py -v
```

Expected: 全 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/llm.py backend/tests/unit/test_langfuse_generation.py
git commit -m "[refactor] LLM 调用包 generation：手填 usage，流式用 finally 保证 end"
```

---

## Task 7: HTTP 中间件 + 5xx 标 ERROR

**Files:**
- Modify: `backend/app/main.py:127-149`
- Create: `backend/tests/integration/test_langfuse_middleware.py`

**Interfaces:**
- Consumes: Task 2 的 `observability.setup/shutdown/current_trace_id`
- Produces: 响应头 `x-trace-id` 与 `traceparent` 携带 Langfuse trace_id；5xx 请求的 span `level=ERROR`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_langfuse_middleware.py`：

```python
"""HTTP 中间件的 trace_id 与 5xx 标记（【方案 §5.3 / §8.3】）。

两种失败模式：
  - 中间件里取不到 trace_id（Starlette 与 SDK 是否同一 OTel context，文档没写）
  - 5xx 没标 level=ERROR → 错误率面板读不到
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import observability
from tests.lf_capture import CapturingLangfuse


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_trace_id_consistent_across_middleware_and_header(capture):
    """★ 中间件内 / 端点内 / 响应头三处必须是同一个 id（实测通过，这里钉住）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)
    seen = {}

    @app.get("/ping")
    async def ping():
        seen["in_endpoint"] = observability.current_trace_id()
        return {"ok": True}

    with TestClient(app) as tc:
        r = tc.get("/ping")

    header_id = r.headers.get("x-trace-id")
    assert header_id and header_id != "0" * 32, "响应头没带 trace_id"
    assert seen["in_endpoint"] == header_id, (
        f"端点内({seen['in_endpoint']}) 与响应头({header_id}) 不一致"
    )


def test_5xx_response_is_marked_error(capture):
    """5xx 必须把 span 标成 `level=ERROR` —— 错误率面板靠它（【方案 §8.3】）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/boom")
    async def boom():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"error": "x"})

    with TestClient(app) as tc:
        tc.get("/boom")

    observability.get_client().flush()
    body = capture.body()
    assert "ERROR" in body, "5xx 没被标成 ERROR —— 错误率面板会恒为 0"


def test_2xx_is_not_marked_error(capture):
    """正控：正常请求不能标 ERROR（否则错误率恒为 100%）。"""
    from app.main import trace_middleware

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(app) as tc:
        tc.get("/ok")

    observability.get_client().flush()
    assert "ERROR" not in capture.body()


def test_unhandled_exception_does_not_leak_message_to_cloud(capture):
    """★ 端点抛异常时，异常 message **不得**进入发出去的字节。

    ⚠️ 实测（Task 5）：Langfuse 内部用 OTel 的 `start_as_current_span`，
       其 `use_span` 默认 `record_exception=True` 且**无参数可关** ——
       异常一旦穿过 with 块，`exception.message`（= `str(e)`）与整个栈都会上传。
       中间件必须在 with 内部吞掉、外部重抛。
    """
    from app.main import trace_middleware

    SECRET_IN_ERROR = "学号2023123456的回显正文"

    app = FastAPI()
    app.middleware("http")(trace_middleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError(SECRET_IN_ERROR)

    with TestClient(app, raise_server_exceptions=False) as tc:
        tc.get("/boom")

    observability.get_client().flush()
    body = capture.body()
    assert body, "没发出任何东西 —— 断言会假通过"
    assert SECRET_IN_ERROR not in body, (
        f"异常 message 被发到了云端：\n{body[:500]}"
    )
```

并在 `backend/app/main.py` 里**不要**加 FastAPI 的全局异常处理器来吞这个 ——
异常必须照常冒泡给上层（`install_handlers` 已有自己的处理），中间件的职责只是不让它穿过观测的 with 块。

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/integration/test_langfuse_middleware.py -v
```

Expected: FAIL（中间件还在用 OTel；不往 Langfuse 发）

- [ ] **Step 3: 改 `main.py`**

把 `backend/app/main.py` 的 `trace_middleware`（L127-149）替换为：

```python
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """trace_id 生成与传播（【方案 §5.3】）。

    请求头无 traceparent 则新建，有则沿用（便于将来接网关或前端串联）。
    """
    client = observability.get_client()

    if client is None:
        # 未启用：照常处理请求，只回一个合法占位 id
        response = await call_next(request)
        response.headers["x-trace-id"] = observability.current_trace_id()
        return response

    carrier = {k.lower(): v for k, v in request.headers.items()}
    trace_id = carrier.get("traceparent", "").split("-")[1] if "traceparent" in carrier else None
    trace_ctx = {"trace_id": trace_id} if trace_id else None

    with client.start_as_current_observation(
        as_type="span",
        name=f"{request.method} {request.url.path}",
        trace_context=trace_ctx,
        input=None, output=None,
    ) as span:
        # ⚠️⚠️ **异常必须在这里吞掉、到 with 外面再重抛**（Task 5 实测发现）：
        #     Langfuse 内部用的是 OTel 的 `start_as_current_span`，其 `use_span`
        #     默认 `record_exception=True`，而 SDK **不暴露关闭它的参数**
        #     （实测 `start_as_current_observation` 的签名里没有 record_exception）。
        #     异常一旦穿过 with，`exception.message`（= `str(e)`，可能回显 prompt）
        #     与整个栈就会发到云端。
        pending: BaseException | None = None
        response = None
        try:
            response = await call_next(request)
        except BaseException as e:  # noqa: BLE001
            span.update(level="ERROR", status_message=type(e).__name__)
            pending = e

        if response is not None:
            span.update(metadata={
                "http.method": request.method,
                "http.route": request.url.path,
                "http.status_code": response.status_code,
            })
            # ★ 5xx 标 ERROR —— Metrics API **没有 HTTP 状态码维度**（实测 29 个
            #    合法维度里没有它），错误率面板靠 level 分组读（【方案 §8.3】）。
            #    ⚠️ 不标的话错误率恒为 0，且不报错。
            if response.status_code >= 500:
                span.update(level="ERROR",
                            status_message=f"HTTP {response.status_code}")

    if pending is not None:
        raise pending          # 出了 with 才重抛 —— 见上面那条警告

    response.headers["x-trace-id"] = observability.current_trace_id()
    return response
```

并把 `lifespan` 里（L102-104）改为：

```python
    # ⚠️ service.name 用**固定 ASCII 名**，不用 `cfg("app.name")`（那是中文显示名）。
    #    实测：中文 service.name 会以乱码出现在每一条标签上。
    observability.setup(service_name="campus-rag")
```

删掉 `from app.core import metrics` / `from app.core import telemetry` 两行 import，加 `from app.core import observability`。

`lifespan` 的 `finally` 里加 `observability.shutdown()`。

`/health` 端点里的 `telemetry.current_trace_id()` 改成 `observability.current_trace_id()`。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/integration/test_langfuse_middleware.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/integration/test_langfuse_middleware.py
git commit -m "[refactor] HTTP 中间件换 Langfuse；5xx 标 level=ERROR 供错误率面板读"
```

---

## Task 8: 全部 telemetry 引用换源（6 个文件）

> ⚠️ **任务边界由 pre-flight 扫描纠正过**：计划初稿只列了 2 个文件，实际 grep 出
> **6 个**引用 `core.telemetry` 的文件。不全部改完，Task 9 删 `telemetry.py` 会直接 import 崩。
> 这 6 个里有 4 个是**同形状的机械替换**（`current_trace_id()` 消费），按 skill 的
> 「batch small same-shape work」一次派发。

**Files:**
- Modify: `backend/app/api/chat.py:17,34`（`reattach` → `attach_trace`）
- Modify: `backend/app/services/document_service.py:26,151`（`reattach` → `attach_trace`）
- Modify: `backend/app/api/documents.py:14,35`（纯 `current_trace_id/span_id`）
- Modify: `backend/app/api/document_access.py:37,96`（纯 `current_trace_id`）
- Modify: `backend/app/core/exceptions.py:66,76,87,99,113`（纯 `current_trace_id`）
- Modify: `backend/app/services/chat_service.py:38,389`（纯 `current_trace_id`）

**Interfaces:**
- Consumes: Task 2 的 `observability.attach_trace()` / `current_trace_id()` / `current_span_id()`
- Produces: 全仓不再有 `core.telemetry` 引用（Task 9 的删除前提）

- [ ] **Step 1: 先看清全部引用点**

```bash
grep -rn "core.telemetry\|core import telemetry" backend/ --include="*.py"
```

Expected: 恰好 6 个文件（chat.py / document_service.py / documents.py /
document_access.py / exceptions.py / chat_service.py）+ 1 个待删的旧测试。
**若多出别的文件，一并按同样方式改掉，并在报告里说明。**

- [ ] **Step 2: 改两处 `reattach`（机制不同，要改调用方式）**

`backend/app/api/chat.py`：import 改为 `from app.core import observability`，然后

```python
    trace_id, span_id = observability.current_trace_id(), observability.current_span_id()

    async def generator():
        with observability.attach_trace(trace_id, span_id):
            async for frame in chat_service.stream_chat(
```

`backend/app/services/document_service.py`：import 改为 `from app.core import observability`，然后

```python
    async def _run() -> None:
        with observability.attach_trace(trace_id, span_id):
```

- [ ] **Step 3: 改四个纯消费点（同形状，机械替换）**

这四处的用法完全一样：把 `from app.core.telemetry import current_trace_id`
（或含 `current_span_id`）换成 `from app.core import observability`，
再把调用点加上 `observability.` 前缀。

| 文件 | 调用点 |
|---|---|
| `api/documents.py` | L35 `trace_id, span_id = current_trace_id(), current_span_id()` |
| `api/document_access.py` | L96 `"trace_id": current_trace_id(),` |
| `core/exceptions.py` | L76/87/99/113 四处 `"trace_id": current_trace_id(),`（**注意 L66 是函数内 import**，也要改） |
| `services/chat_service.py` | L389 `trace_id=current_trace_id(),` |

> ⚠️ `exceptions.py` 的 import 在 `install_handlers()` **函数体内**（L66），不是文件顶部 —— 别只改顶部。

- [ ] **Step 4: 确认换干净了**

```bash
grep -rn "core.telemetry\|core import telemetry" backend/app/ --include="*.py"
```

Expected: **无输出**

```bash
grep -rn "current_trace_id\|current_span_id" backend/app/ --include="*.py"
```

Expected: 只剩 `core/observability.py` 里的定义，以及各调用点的 `observability.xxx` 形式。

- [ ] **Step 5: 补 `attach_trace` 的单元测试**

> ⚠️ Task 2 审查时发现：`attach_trace` 是**取代 `reattach` 的机制**，却只被 T14 的人工验收
> A8 覆盖，没有任何自动化测试。机制本身已在方案实测中验过（`trace_context` 能把新观测
> 挂回原 trace，【方案 §12.1】#2），但**我们自己的包装层**没测。

新建 `backend/tests/unit/test_langfuse_attach_trace.py`：

```python
"""后台任务续接（【方案 §5.4】）—— 取代 `telemetry.reattach()`。

⚠️ 「后台任务不能靠 contextvars 隐式继承」这条教训不变（§3.2.3.1）；
   换的是实现手段（Langfuse 的 `trace_context`），不是要不要显式传递。
"""
from __future__ import annotations

import pytest

from app.core import observability
from tests.lf_capture import CapturingLangfuse


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_attach_trace_joins_existing_trace(capture):
    """★ 挂在别的任务里的观测，trace_id 必须与原 trace 相同。"""
    client = observability.get_client()

    with client.start_as_current_observation(as_type="span", name="root-req"):
        root_trace = observability.current_trace_id()
        assert root_trace != "0" * 32

        # 模拟后台任务：脱离当前上下文后，用 attach_trace 挂回去
        with observability.attach_trace(root_trace, observability.current_span_id()):
            inner = observability.current_trace_id()

    assert inner == root_trace, (
        f"后台观测没挂回原 trace：{inner} != {root_trace}"
    )


def test_attach_trace_is_noop_when_disabled(monkeypatch):
    """未启用时必须是安全的空操作，不能抛。"""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    observability._reset_for_test()
    observability.setup("campus-rag")

    with observability.attach_trace("a" * 32, "b" * 16):
        pass          # 不抛即通过
```

- [ ] **Step 6: 跑相关回归**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/integration/test_stage_events.py backend/tests/integration/test_session_lock.py backend/tests/integration/test_pipeline.py backend/tests/integration/test_document_access_acl.py -v
```

Expected: 全 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/chat.py backend/app/services/document_service.py backend/app/api/documents.py backend/app/api/document_access.py backend/app/core/exceptions.py backend/app/services/chat_service.py backend/tests/unit/test_langfuse_attach_trace.py
git commit -m "[refactor] 全部 telemetry 引用换源（6 文件）：SSE/后台任务走 trace_context，其余走 observability"
```

---

## Task 9: 删除旧栈

**Files:**
- Delete: `backend/app/core/telemetry.py`、`backend/app/core/metrics.py`
- Delete: `otel-collector-config.yaml`、`prometheus.yml`、`prometheus-rules.yml`
- Modify: `docker-compose.yml`、`scripts/start.sh`

- [ ] **Step 1: 确认没有残留引用**

⚠️ **判据要精确到 import**（Task 8 踩过）：`observability.py` 的模块 docstring 里有
**散文提及** `core/telemetry.py`，用宽 grep 会把它算进去、看起来像"还有引用"。

```bash
grep -rnE "^\s*(from|import)\s.*telemetry|from app\.core\.telemetry|app\.core\.telemetry" backend/app/ --include="*.py"
```

Expected: **无输出**。有输出就先改完再删 —— 否则删了会 import 崩。

另跑一遍宽 grep 看**注释里**的过时提及（这些不影响运行，但会误导后来人）：

```bash
grep -rn "telemetry\.py\|record_llm_tokens" backend/app/ backend/tests/ --include="*.py"
```

Expected: 只剩 `core/observability.py` 顶部的历史说明（那是**有意保留**的散文，见该文件 docstring）。
其余（Task 8 报告点名了 `core/logging.py:8`、`core/metrics.py:12`、`tests/unit/test_langfuse_node_span.py:77`）
**在本步一并清掉** —— 它们指向的文件马上就不存在了。`metrics.py` 本身在本任务删除，其注释随之消失。

- [ ] **Step 2: 删代码文件**

```bash
git rm backend/app/core/telemetry.py backend/app/core/metrics.py
```

- [ ] **Step 3: 删三个配置文件**

```bash
git rm otel-collector-config.yaml prometheus.yml prometheus-rules.yml
```

- [ ] **Step 4: 改 `docker-compose.yml`**

删掉 `jaeger` / `otel-collector` / `prometheus` 三个 service 块与末尾的 `promdata:` volume 声明。**保留** `postgres` 与 `pgdata`。

同时把文件顶部的注释改掉（现在写的是「M5 会追加三个可观测容器」）——改为：

```yaml
# 校园 RAG 系统 — 服务编排
#
# 本文件只有 PostgreSQL（附录 G）。
# 可观测性走 Langfuse 云，**不再自建容器**（见
# docs/superpowers/specs/2026-10-11-langfuse-observability-design.md）。
#
# 启动：docker compose up -d --wait postgres
# 应用启动硬依赖 Docker daemon —— 演示/答辩前先确认 Docker Desktop 已启动。
```

- [ ] **Step 5: 改 `scripts/start.sh`**

删掉 L23-44（起容器 + 等 Collector 就绪那段），改为：

```bash
echo "==> 起 PostgreSQL"
docker compose up -d --wait postgres

if [ "${1:-}" = "--infra-only" ]; then
  echo "==> 只起基础设施，完成"
  exit 0
fi
```

文件头的注释里「可观测三件套」改为「可观测性走 Langfuse 云（无需本地容器）」。

- [ ] **Step 6: 清理 `.env` 与 `.env.example`**

**`.env`（本地，不进版本库）**：删掉 `OTEL_EXPORTER_OTLP_ENDPOINT` 那两行注释与那一行值。
**保留**已加好的 `LANGFUSE_*` 三行。

**`.env.example`（进版本库）**：把第 41 行的 `OTEL_EXPORTER_OTLP_ENDPOINT=...`
替换为占位（**绝不写真实密钥**）：

```bash
# 可观测性（Langfuse 云）
# ⚠️ LANGFUSE_BASE_URL 必须显式配 —— SDK 缺省值是 https://cloud.langfuse.com，
#    只配 key 不配地址会把数据发去公有云。
# ⚠️ 两个 key 都不配 = 完全不发外网（跑测试、跑 CLI 的正常状态）。
LANGFUSE_PUBLIC_KEY=""
LANGFUSE_SECRET_KEY=""
LANGFUSE_BASE_URL="https://cloud.langfuse.com"
```

- [ ] **Step 7: 验证服务能起**

```bash
cd /d/Knowledge_rag_system
bash scripts/start.sh --infra-only
docker compose config --quiet && echo "compose 语法 OK"
```

Expected: 无报错

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "[chore] 删除 OTel 自建栈：telemetry.py/metrics.py/三容器/三配置"
```

---

## Task 10: 仪表盘改读 Metrics API v2

**Files:**
- Modify: `backend/app/services/stats_service.py:34-42,170-270`
- Modify: `backend/app/config/app.yaml`（加阈值段）
- Create: `backend/tests/unit/test_langfuse_metrics_query.py`

**Interfaces:**
- Consumes: Task 1 的 `langfuse`（只为 HTTP 客户端无关，实际用 `httpx` 直连）
- Produces: `retrieval_metrics()` 返回字段不变 + 新增 `cost`、`token_input`、`token_output`、`node_latency`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_langfuse_metrics_query.py`：

```python
"""Metrics API v2 查询构造（【方案 §8.1】）。

**形状全部是实测来的**（【方案 §12.4】验收 D）：
  - `query` 是**一个 JSON 字符串参数**，不是散装参数
  - `fromTimestamp` / `toTimestamp` **必填**
  - 返回字段是 `<聚合>_<指标>` 拼接
  - 空结果有**两种形态**：`[{"count_count":0,"p95_latency":null}]` 与 `[]`
    —— 前者 latency 是 `null` 不是 0，直接 float() 会抛异常
"""
from __future__ import annotations

import json

import pytest

from app.services import stats_service


def test_query_is_a_single_json_param():
    q = stats_service._build_metrics_query(
        measures=[("latency", "p95")],
        dimensions=None, hours=24,
    )
    assert isinstance(q, str)
    parsed = json.loads(q)
    assert parsed["view"] == "observations"
    assert parsed["metrics"] == [{"measure": "latency", "aggregation": "p95"}]
    assert "fromTimestamp" in parsed and "toTimestamp" in parsed, "时间戳必填"


def test_query_with_dimension():
    q = stats_service._build_metrics_query(
        measures=[("latency", "p95")],
        dimensions=["name"], hours=24,
    )
    assert json.loads(q)["dimensions"] == [{"field": "name"}]


@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"p95_latency": 1234.5}]}, 1234.5),
    ({"data": [{"p95_latency": None}]}, None),        # ← 空结果的形态一
    ({"data": []}, None),                              # ← 空结果的形态二
    ({"data": [{}]}, None),
])
def test_extract_scalar_handles_both_empty_shapes(payload, expected):
    """★ 两种空结果都不能抛异常（【方案 §8.1】记的那个坑的放大版）。"""
    assert stats_service._extract_scalar(payload, "p95_latency") == expected


def test_extract_scalar_keeps_real_zero():
    """正控：真的是 0 时要返回 0，不能跟「没数据」混为一谈。"""
    assert stats_service._extract_scalar({"data": [{"count_count": 0}]},
                                         "count_count") == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_metrics_query.py -v
```

Expected: FAIL — `AttributeError: module ... has no attribute '_build_metrics_query'`

- [ ] **Step 3: 改 `stats_service.py`**

把文件头注释里「运行指标用 PromQL 查 Prometheus」改为「运行指标用 Metrics API v2 查 Langfuse（【方案 §8.1】）」。

把 L34-42 的 Prometheus 配置段替换为：

```python
# Langfuse 配置。⚠️ 实测 v1 接口对本组织全部 410，一切走 v2（【方案 §12.4】）。
LANGFUSE_BASE_URL: str = (os.environ.get("LANGFUSE_BASE_URL", "").strip()
                          or cfg("observability.langfuse_base_url", "") or "")
LANGFUSE_PUBLIC_KEY: str = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
LANGFUSE_SECRET_KEY: str = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()

# 测试注入用（httpx.MockTransport）—— 让「答得上」这条分支走**真实 HTTP 代码路径**
_lf_transport: httpx.BaseTransport | None = None

# 超时要短：仪表盘是给人看的，Langfuse 慢不该拖垮页面
_LF_TIMEOUT = 3.0
```

把 L170 附近的 `_prom_query` 整个替换为：

```python
def _build_metrics_query(*, measures: list[tuple[str, str]],
                         dimensions: list[str] | None, hours: int) -> str:
    """拼 Metrics API v2 的 `query` 参数（**一个 JSON 字符串**，实测形状）。

    ⚠️ `fromTimestamp` / `toTimestamp` 是**必填**的（不传直接 400）。
    ⚠️ 窗口一律取「相对当前时间」—— 实测传很久以前的绝对日期会报
       「fromTimestamp must be before toTimestamp」（**报错信息有误导性**）。
    """
    now = datetime.now(timezone.utc)
    query: dict = {
        "view": "observations",
        "metrics": [{"measure": m, "aggregation": a} for m, a in measures],
        "fromTimestamp": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if dimensions:
        query["dimensions"] = [{"field": d} for d in dimensions]
    return json.dumps(query)


def _extract_scalar(payload: dict | None, key: str) -> float | None:
    """从 v2 返回里取一个标量。

    ⚠️ **两种空结果都要扛住**（实测）：
       ① `{"data":[{"count_count":0,"p95_latency":null}]}` —— 字段是 `null`
       ② `{"data":[]}` —— 连行都没有
       直接 `float(row[key])` 会在①上抛 TypeError，被 swallow 后变成静默的「—」。
    """
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    value = rows[0].get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _lf_query(query_json: str) -> dict | None:
    """查一次 Metrics API v2。任何失败返回 None（由调用方决定怎么降级）。"""
    if not (LANGFUSE_BASE_URL and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        return None
    try:
        async with httpx.AsyncClient(
            base_url=LANGFUSE_BASE_URL,
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            timeout=_LF_TIMEOUT, transport=_lf_transport,
        ) as client:
            resp = await client.get("/api/public/v2/metrics",
                                    params={"query": query_json})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001 —— 运行指标缺失不该让页面挂掉
        logger.warning("Langfuse 查询失败：%s", e,
                       extra={"event": "stats.langfuse_failed"})
        return None
```

把 `retrieval_metrics()`（L249-270）替换为：

```python
async def retrieval_metrics() -> dict:
    """运行指标（§4.4）—— 唯一读 Langfuse 的接口。

    ⚠️ 不可用时返回 `available: False`，**api 层仍返 HTTP 200**（M4-D5）。
    ⚠️ token 用**累计值**不用 rate：问答是稀疏事件，窗口两端数值常常相同，
       rate 会算成 0（实测踩过 —— 指标明明有 331，rate 报 0）。
    """
    if not (LANGFUSE_BASE_URL and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        return {"available": False, "status": "langfuse_unavailable"}

    hours = int(cfg("observability.metrics_window_hours", 24))

    lat = await _lf_query(_build_metrics_query(
        measures=[("latency", "p50"), ("latency", "p95")],
        dimensions=None, hours=hours))
    # 5xx 率：实测**没有 HTTP 状态码维度**，改用 level=ERROR（【方案 §8.3】）
    err = await _lf_query(_build_metrics_query(
        measures=[("count", "count")], dimensions=["level"], hours=hours))
    # ⚠️ **不要给 token 加 `dimensions=["usageType"]`** —— 实测（2026-10-11）那个维度
    #    返回的 total/input/output 三行是**同一个总量的副本**：既拆不开，全加还会报 3 倍。
    #    真正的拆分靠三个**独立 measure**（`inputTokens`/`outputTokens`/`totalTokens`），
    #    返回正确的 7 / 3 / 10。下面这版就是实测确认过的形状。
    tok = await _lf_query(_build_metrics_query(
        measures=[("inputTokens", "sum"), ("outputTokens", "sum"),
                  ("totalTokens", "sum"), ("totalCost", "sum")],
        dimensions=None, hours=hours))
    nodes = await _lf_query(_build_metrics_query(
        measures=[("latency", "p95")], dimensions=["name"], hours=hours))

    values = {
        "latency_p50": _extract_scalar(lat, "p50_latency"),
        "latency_p95": _extract_scalar(lat, "p95_latency"),
        "error_rate": _error_rate_from_levels(err),
        # 三个 measure 都从**同一行**读（`_extract_scalar` 只取 `data[0]`）——
        # 绝不能退化成按行求和。
        "token_usage": _extract_scalar(tok, "sum_totalTokens"),
        "token_input": _extract_scalar(tok, "sum_inputTokens"),
        "token_output": _extract_scalar(tok, "sum_outputTokens"),
        # ⚠️ 实测恒为 0：Langfuse 项目里没配 deepseek-flash 的价格
        #    （`/api/public/models` 50 条内置模型里没有 deepseek）。
        #    修法是在 Langfuse 后台加模型定义，**不是代码**。
        "cost": _extract_scalar(tok, "sum_totalCost"),
        "node_latency": _node_latency_rows(nodes),
    }

    alerts = _evaluate_alerts(values)

    if all(v is None for k, v in values.items() if k != "node_latency") and not values["node_latency"]:
        return {"available": False, "status": "langfuse_unavailable"}

    return {"available": True, **values, "alerts": alerts}
```

再补三个小工具函数与告警自算（Task 11 也会用到，这里先写好）：

```python
# ⚠️ 原稿在这里有个 `_sum_rows()`（把返回的每一行加起来）。
#    **已删除，不要再加回来** —— 实测：`usageType` 维度返回的 total/input/output
#    三行是同一个总量，按行求和会把 token 用量报成 3 倍（Task 10 实测到 `12963 == 4321*3`）。
#    现在一律用 `_extract_scalar()` 从**单行**取字段。


def _error_rate_from_levels(payload: dict | None) -> float | None:
    """按 level 分组算错误率（【方案 §8.3】实测的替代方案）。"""
    rows = (payload or {}).get("data") or []
    total = sum(r.get("count_count") or 0 for r in rows)
    if not total:
        return None
    errs = sum(r.get("count_count") or 0 for r in rows if r.get("level") == "ERROR")
    return errs / total


def _node_latency_rows(payload: dict | None) -> list[dict]:
    """按节点分组的 p95（§8.2 新增面板）。"""
    rows = (payload or {}).get("data") or []
    out = []
    for r in rows:
        name = r.get("name") or ""
        val = r.get("p95_latency")
        if name.startswith("node.") and val is not None:
            out.append({"node": name[len("node."):], "p95": float(val)})
    return sorted(out, key=lambda x: x["p95"], reverse=True)


def _evaluate_alerts(values: dict) -> dict:
    """告警后端自算（【方案 §8.3】方案 A）。

    ⚠️ **不用 Langfuse Alerts**：实测告警没有公开 API（6 个候选端点全 404），
       「后端读告警状态」这条路走不通。改为后端拿指标自己比阈值。

    ⚠️ 返回形状与改造前**保持一致**（`{firing, pending, rules:[{name,state}]}`），
       前端告警卡不用改。

    ⚠️ 值为 None（无数据）时**不触发** —— 刚起服务、还没有流量是正常状态，
       误报会让人把「已接好、只是还没流量」当成故障。

    阈值取自 `observability.alert_thresholds`（Task 11 加进 app.yaml）。
    """
    thresholds = cfg("observability.alert_thresholds", {}) or {}
    node_p95_ms = float(thresholds.get("node_p95_ms", 10000))
    err_threshold = float(thresholds.get("http_5xx_rate", 0.05))

    rules: list[dict] = []

    # 规则一：按节点的 p95 越限（原 prometheus-rules.yml 的 RAGNodeP95High，
    # 那条因为指标名对不上而**从来没触发过**，【方案 §1.1】）
    for row in values.get("node_latency") or []:
        state = "firing" if row["p95"] > node_p95_ms else "inactive"
        rules.append({"name": f"RAGNodeP95High[{row['node']}]", "state": state})

    # 规则二：5xx 比例（原按 http_status_code 过滤；现靠 level=ERROR，
    # 因为实测 Metrics API **没有 HTTP 状态码维度**）
    rate = values.get("error_rate")
    rules.append({
        "name": "RAGHttp5xxRate",
        "state": ("firing" if rate is not None and rate > err_threshold
                  else "inactive"),
    })

    return {
        "firing": sum(1 for r in rules if r["state"] == "firing"),
        "pending": 0,          # 自算没有 pending 态（原 Prometheus 的 for: 5m 概念）
        "rules": rules,
    }
```

文件顶部 import 补：`import json`、`from datetime import timezone`（`timedelta` 已有）。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_metrics_query.py -v
```

Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/stats_service.py backend/tests/unit/test_langfuse_metrics_query.py
git commit -m "[refactor] 仪表盘运行指标改读 Langfuse Metrics API v2（含两种空结果形态）"
```

---

## Task 11: 告警后端自算（方案 A）

**Files:**
- Modify: `backend/app/services/stats_service.py`（`_prom_rules` → `_evaluate_alerts`）
- Modify: `backend/app/config/app.yaml`
- Modify: `backend/app/schemas/stats.py`
- Create: `backend/tests/unit/test_langfuse_alerts.py`

**Interfaces:**
- Consumes: Task 10 的 `retrieval_metrics()`
- Produces: `_evaluate_alerts(values: dict) -> dict` —— 返回 `{firing, pending, rules: [{name, state}]}`，**与改造前同形状**（前端不用改这块）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_langfuse_alerts.py`：

```python
"""告警后端自算（【方案 §8.3】方案 A）。

**为什么不用 Langfuse Alerts**：实测告警**没有公开 API**（6 个候选端点全 404），
所以「后端读告警状态 → 仪表盘标红」这条路走不通，改为后端拿指标自己比阈值。
"""
from __future__ import annotations

from app.services import stats_service


def test_no_firing_when_all_normal():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.0, "node_latency": [],
    })
    assert result["firing"] == 0
    assert isinstance(result["rules"], list)


def test_node_p95_over_threshold_fires():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.0,
        "node_latency": [{"node": "rerank", "p95": 15000.0}],
    })
    assert result["firing"] >= 1
    names = [r["name"] for r in result["rules"] if r["state"] == "firing"]
    assert any("rerank" in n for n in names)


def test_error_rate_over_threshold_fires():
    result = stats_service._evaluate_alerts({
        "latency_p95": 100.0, "error_rate": 0.9, "node_latency": [],
    })
    firing = [r["name"] for r in result["rules"] if r["state"] == "firing"]
    assert any("Http" in n or "Error" in n for n in firing)


def test_none_values_do_not_fire():
    """没有数据时不能误报 —— 刚起服务、还没有流量是正常状态。"""
    result = stats_service._evaluate_alerts({
        "latency_p95": None, "error_rate": None, "node_latency": [],
    })
    assert result["firing"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_alerts.py -v
```

Expected: FAIL — `_evaluate_alerts` 不存在

- [ ] **Step 3: 在 `app.yaml` 加阈值，并删掉死键**

⚠️ **先删掉 `observability.prometheus_url` 那一条及其注释**（Task 10 报告指出）：
`stats_service.py` 已不再读它，Prometheus 容器也已删除 —— 留着是一条**指向不存在服务的配置**，
下一个人会以为还有东西在用它。

在 `observability` 段下追加：

```yaml
  # 运行指标窗口（小时）。⚠️ 一律用「相对当前时间」，不要配绝对日期
  #    —— 实测传很久以前的绝对日期会被 API 拒（报错信息有误导性）
  metrics_window_hours: 24
  # 告警阈值（【方案 §8.3】方案 A：后端自算，不依赖 Langfuse Alerts API）
  # ⚠️ 阈值是初值，M5 用真实流量标定后才作数
  alert_thresholds:
    node_p95_ms: 10000        # 单节点 p95 超过 10 秒
    http_5xx_rate: 0.05       # 5xx 比例超过 5%
```

- [ ] **Step 4: 确认 `_prom_rules` 已删干净**

> **注意**：`_evaluate_alerts()` 的**实现已经在 Task 10 里写好了**（本任务只负责
> 阈值配置与专项测试）。之所以这样切分：Task 10 的 `retrieval_metrics()` 会调用它，
> 若定义留到本任务，Task 10 交付的就是个「测试全绿但一调用就 NameError」的函数。

⚠️ **判据要用代码级 grep，不要用宽 grep**（Task 10 踩过）：`_evaluate_alerts()` 的注释里
**有意保留了**解释历史的 `prometheus-rules.yml` / `Prometheus` 字样（说明这条规则为什么与
原来那条不同）。宽 grep 会把它们算进去、看起来像还有残留 —— 那是**注释，不是代码**。

```bash
grep -nE "_prom_rules|prometheus_url|PROMETHEUS_URL|/api/v1/rules" backend/app/services/stats_service.py
```

Expected: **无输出**。有残留就删掉 —— 读 `/api/v1/rules` 与 Prometheus 基址的那几段都不再需要。

再跑一遍宽 grep 只为了**看清有哪些是注释**：

```bash
grep -n "prometheus\|Prometheus" backend/app/services/stats_service.py
```

Expected: 只剩注释里的历史说明（有意保留）。**不要删它们** —— 它们解释了
「为什么 5xx 用 level 而不是状态码维度」这类实测得来的决定。

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/unit/test_langfuse_alerts.py -v
```

Expected: 4 passed

- [ ] **Step 6: 跑统计相关回归**

```bash
.venv/Scripts/python.exe -m pytest backend/tests/integration/test_admin_stats.py -v
```

Expected: 全 passed。**若有失败**，多半是它 mock 了 Prometheus 的 transport —— 把 mock 目标从 `_prom_transport` 改成 `_lf_transport`。

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/stats_service.py backend/app/config/app.yaml backend/tests/unit/test_langfuse_alerts.py
git commit -m "[feat] 告警改后端自算（方案 A）：阈值配置化，替代读不到的 Langfuse Alerts"
```

---

## Task 12: 前端仪表盘

> ⚠️ **顺带清掉三处过时的 Prometheus 措辞**（Task 11 报告点名，宽 grep 在验收时会命中它们）：
> - `backend/app/api/stats.py:5`（"Prometheus 不可用"）、`:55`（"PromQL 查 Prometheus"）
> - `backend/app/schemas/stats.py:71`（"Prometheus 规则，非 Alertmanager"）
>
> 它们现在是**错的**：运行指标读的是 Langfuse Metrics API v2，告警是后端自算的。
> ⚠️ **但 `stats_service.py` 里那两处 Prometheus 字样要留着** —— 那是解释历史的注释
> （说明为什么 5xx 用 `level` 而不是状态码维度）。**改错地方会把有用的注释删掉。**
>
> ⚠️ 另补一处**配置注释的误导**（Task 11 审查指出）：`app.yaml` 里
> `http_5xx_rate` 的注释写着「5xx 比例超过 5%」，但该阈值实际拦的是
> **`level=ERROR` 的观测占比**（Metrics API 没有 HTTP 状态码维度）。
> 加一句「实际是 ERROR 级观测占比」即可 —— 读这份配置的人看不到代码里的解释。
> **不要改键名**：名字继承自规则名 `RAGHttp5xxRate`，是前端契约的一部分。

**Files:**
- Modify: `frontend/web/src/api/schema.d.ts`（重新生成）
- Modify: `frontend/web/src/views/admin/AdminDashboard.tsx:144-199`
- Modify: `backend/app/api/stats.py`（两处过时措辞）
- Modify: `backend/app/schemas/stats.py`（加字段 + 一处过时措辞）

**Interfaces:**
- Consumes: Task 10/11 的 `/api/admin/stats/retrieval` 新字段
- Produces: 四个新面板（成本 / token 拆分 / 按节点 p95）+ 深链提示改指 Langfuse

- [ ] **Step 1: 先改后端 schema**

在 `backend/app/schemas/stats.py` 的 `RetrievalMetricsResponse` 里追加：

```python
    #: LLM 成本（【方案 §8.2】新增，改造前完全没有）
    cost: float | None = None
    #: token 拆输入/输出（【方案 §8.2】新增；改造前只有一个合计）
    #: ⚠️ 数据来自**三个独立 measure**（`inputTokens`/`outputTokens`/`totalTokens`），
    #:    **不是** `usageType` 维度 —— 实测那个维度返回的三行是同一个总量的副本，
    #:    既拆不开、全加还会报 3 倍（Task 10 踩过两次）
    token_input: float | None = None
    token_output: float | None = None
    #: 按节点的 p95 耗时（§8.2 新增；把 §4.4 表里「按节点分组」那条真正做出来）
    node_latency: list[NodeLatency] = []
```

并新增：

```python
class NodeLatency(BaseModel):
    node: str
    p95: float
```

- [ ] **Step 2: 重新生成前端类型**

⚠️ `gen:api` 脚本是 `openapi-typescript http://127.0.0.1:8090/openapi.json -o src/api/schema.d.ts`
—— **需要后端正在跑**（端口 8090），先 `bash scripts/start.sh` 或单独起 uvicorn。

```bash
cd /d/Knowledge_rag_system/frontend/web && npm run gen:api
grep -c "node_latency" src/api/schema.d.ts   # 期望 ≥1，否则后端没重新加载
```

- [ ] **Step 3: 改 `AdminDashboard.tsx` 的运行指标区块**

把 L144-170 那段（`{retrieval?.available ? (...) : (...)}`）替换为：

```tsx
          {retrieval?.available ? (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Stat title="延迟 p50（ms）" value={retrieval.latency_p50 ?? '—'} />
              <Stat title="延迟 p95（ms）" value={retrieval.latency_p95 ?? '—'} />
              <Stat title="错误率" value={retrieval.error_rate ?? '—'} />
              <Stat title="token 用量" value={retrieval.token_usage ?? '—'}
                    hint={`输入 ${retrieval.token_input ?? '—'} / 输出 ${retrieval.token_output ?? '—'}`} />
              {/* —— §8.2 新增：Langfuse 白捡的三项 —— */}
              <Stat title="LLM 成本" value={retrieval.cost ?? '—'} hint="按模型自动折算" />
              <AlertCard alerts={retrieval.alerts} />
              <NodeLatencyCard rows={retrieval.node_latency ?? []} />
              <p className="col-span-2 text-xs text-muted-foreground lg:col-span-4">
                下钻到 Langfuse（<code>{'https://cloud.langfuse.com'}</code>）看单次请求
                的具体环节：搜这条回答响应头里的 <code>x-trace-id</code> 即可。
              </p>
            </div>
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">
              运行指标暂不可用
              {retrieval?.status ? `（${retrieval.status}）` : ''} ——
              业务指标不受影响。可观测性由 Langfuse 云提供。
            </p>
          )}
```

并在文件末尾追加 `NodeLatencyCard` 组件：

```tsx
/** 按节点的 p95 耗时（【方案 §8.2】新增 —— §4.4 要求「按节点分组」，改造前从未实现）。 */
function NodeLatencyCard({ rows }: { rows: { node: string; p95: number }[] }) {
  if (!rows.length) {
    return <Stat title="节点耗时 p95" value="—" hint="暂无数据" />
  }
  const worst = rows[0]
  return (
    <Card className="col-span-2 lg:col-span-4">
      <CardContent className="pt-6">
        <p className="text-sm text-muted-foreground">节点耗时 p95（ms）</p>
        <div className="mt-2 space-y-1">
          {rows.slice(0, 8).map(r => (
            <div key={r.node} className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{r.node}</span>
              <span className={r === worst ? 'font-semibold' : ''}>
                {Math.round(r.p95)}
              </span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
```

- [ ] **Step 4: 前端构建通过**

```bash
cd /d/Knowledge_rag_system/frontend/web && npx tsc --noEmit && npm run build
```

Expected: 无 TS 错误、构建成功

- [ ] **Step 5: Commit**

```bash
git add frontend/web/src backend/app/schemas/stats.py
git commit -m "[feat] 仪表盘：新增成本与按节点 p95 面板，深链提示改指 Langfuse"
```

---

## Task 13: 主文档修订

**Files:**
- Modify: `docs/校园RAG系统重构方案.md`

**Interfaces:**
- Consumes: 全部前置任务的实际改动
- Produces: 文档与实现一致

- [ ] **Step 1: 按【方案 §11】修订清单逐条改**

打开 `docs/superpowers/specs/2026-10-11-langfuse-observability-design.md` 的 **§11 主文档修订清单**，**逐行**改 `docs/校园RAG系统重构方案.md`。共 11 处。

⚠️ **两处必须同时改**，只改一处会让文档自相矛盾：
- §3.2.3.6 的「商业 APM：数据不应出网」
- 第 4433 行的「数据不出网 ✅ 每段检索文本只在本地处理」

统一改为「**标识符不出网**：正文经脱敏钩子处理后才上传 Langfuse，学号/手机号/身份证在本地打码」。

- [ ] **Step 2: 全文搜残留**

```bash
grep -n "Prometheus\|Jaeger\|otel-collector\|OTEL_EXPORTER" docs/校园RAG系统重构方案.md | head -30
```

Expected: 只在**说明历史决策**的地方出现（如「为什么一度选 OTel」），其余全部改掉。逐条判断。

- [ ] **Step 3: Commit**

```bash
git add docs/校园RAG系统重构方案.md
git commit -m "[docs] 主文档同步改造：§3.2.3 整章、§4.4 面板表、「数据不出网」改口径"
```

---

## Task 14: 端到端验收（A / B / C）

**这是本计划的收尾，也是你要求的「施工完成后检查功能完整性」。**

**前置条件**：
- LLM API Key 可用（会真实消耗 token）
- 精排模型预热完成 —— 等日志里出现 `rerank.preloaded`（实测约 29 秒，没完就提问会撞在加载锁上等）

- [ ] **Step 1: 起服务**

```bash
cd /d/Knowledge_rag_system
bash scripts/start.sh
```

另开一个终端跑前端：

```bash
cd /d/Knowledge_rag_system/frontend/web && npx vite --port 5273
```

- [ ] **Step 2: 验收 A —— Langfuse 链路（8 项逐条打勾）**

登录 → 提一个**知识型问题**（不能是寒暄，那会被 route 分流走）→ 等流式回答结束 → 去 `https://cloud.langfuse.com` 核对：

- [ ] **A1** 按时间范围能查到这一次 trace
- [ ] **A2** 是**一棵树**：HTTP span 为根 → 图节点 span 为子 → generation 为孙，父子关系正确
- [ ] **A3** 图节点**齐全**：实际走过的都在（`node.resolve` / `route` / `retrieve` / `rerank` / `generate` / `cite`），不重不漏
- [ ] **A4** 每个 LLM 调用是 **generation**，input/output token **都不是 0**
- [ ] **A5** 一次 SSE 流**全程同一个 trace_id**（流内事件没分裂成多条 trace）
- [ ] **A6** **四处 trace_id 一致**：响应头 `x-trace-id` == `logs/app.jsonl` 里的 `trace_id` == `qa_logs.trace_id` == Langfuse UI 显示的
- [ ] **A7** **脱敏生效**：造一条**含 10 位假学号**的提问去跑，Langfuse 里该学号**已打码**
- [ ] **A8** **后台任务挂回原 trace**：上传文档触发解析，解析的 span 挂在**那次上传请求**的 trace 下

> ⚠️ A7 是决策 3 的唯一验收方式，也是唯一能证明「脱敏不是自我声明」的一条。
> A8 单独跑 —— 它走的是另一条代码路径（`attach_trace`）。

- [ ] **Step 3: 验收 B —— 仪表盘（8 项）**

admin 登录 → 打开仪表盘：

- [ ] **B1** 核心指标卡有值（文档数/chunk 数/问答量/拒答率）
- [ ] **B2** 延迟 p50 / p95 有值，**不是 NaN、不是「—」**
- [ ] **B3** 错误率有值
- [ ] **B4** token 用量有值
- [ ] **B5** **LLM 成本**有值（新增面板）
      ⚠️ **已知前置**：Task 10 实测 `totalCost`/`inputCost`/`outputCost` **都是 0** ——
      大概率是 Langfuse 项目里**没配 DeepSeek 的模型价格**（不是代码问题）。
      若确实如此，在 Langfuse 后台「Settings → Models」加上 `deepseek-flash` 的单价后再验；
      **在此之前 B5 记「有值但恒 0」并注明原因**，不要当成通过。
- [ ] **B6** **节点耗时 p95** 有值且按节点分行（新增面板）
- [ ] **B7** 告警状态卡正常显示，不报错
- [ ] **B8** 业务指标区块照常渲染（趋势/拒答分布/高频问题/降级次数）

- [ ] **Step 4: 验收 B8 —— 降级路径（必须单独验）**

把 `.env` 的 `LANGFUSE_BASE_URL` 改成一个**不存在**的地址 → 重启后端 → 重开仪表盘：

- [ ] 运行指标区块显示「运行指标暂不可用（langfuse_unavailable）」
- [ ] **业务指标区块照常渲染**
- [ ] 接口返 **HTTP 200 不是 500**（`curl -s -o /dev/null -w "%{http_code}"` 确认）
- [ ] **提问主链路不受影响**（还能正常问答）

验完**改回正确地址**。

- [ ] **Step 5: 验收 C —— 静默失败边界（3 项）**

- [ ] **C1** 把两个 `LANGFUSE_*` key 注释掉 → 重启 → 提一次问 → 看 `logs/app.jsonl`：`trace_id` 是 `"000…0"`（32 位），**不是 `null`**
- [ ] **C2** 配好密钥重启后，数据出现在**你自己的项目** `Knowledge_rag_system` 里（不是别的项目）
- [ ] **C3** 提一个**走流式**的问题 → Langfuse 里 `generate` 的 generation **存在**且 **usage 有值**

验完**改回正确配置**。

- [ ] **Step 6: 跑全量测试收尾**

```bash
cd /d/Knowledge_rag_system
.venv/Scripts/python.exe -m pytest -q 2>&1 | tail -20
```

Expected: 与 Task 1 Step 1 的基线**一致或更好**（新增测试全绿，旧测试没有新失败）

- [ ] **Step 7: 提交验收记录**

在 `docs/superpowers/specs/2026-10-11-langfuse-observability-design.md` 的 §12.4 末尾追加一节 `#### 验收结果（YYYY-MM-DD 实际执行）`，逐条记录 A1-A8 / B1-B8 / C1-C3 的实测结果，然后把任务清单勾完。

```bash
git add docs/
git commit -m "[docs] 端到端验收 A/B/C 执行完毕，结果记录在案"
```

---

## 附录：施工注意

**必须停下来的情况**（不要硬扛）：

| 情况 | 怎么办 |
|---|---|
| Task 1 Step 5 出现 chromadb 相关的新失败 | OTel 升级引起，**停下报告** |
| Task 3 `test_non_sensitive_text_survives` 失败 | 正则太宽吃了正常中文，**修正则不要删测试** |
| Task 5 测试里 `body` 始终为空 | 说明数据根本没发出来，先查 `setup()` 是否真的启用了 |
| 任何一步「实测结果与方案记录不符」 | **以实测为准**，改方案文档并说明 |

**提交规范**：`[类型] 简述`，类型用 `feat` / `fix` / `docs` / `chore` / `refactor` / `deps`。
**每条提交只进 `refactor/campus-rag` 分支，main 不动。**
