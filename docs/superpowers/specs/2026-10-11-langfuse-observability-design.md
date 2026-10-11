# 可观测性改造：OTel 自建栈 → Langfuse 云 — 设计方案

> **日期**：2026-10-11 ｜ **分支**：`refactor/campus-rag` ｜ **状态**：待负责人审阅
>
> **依据**：负责人指定的四个决定（见 §2）+ Langfuse 官方文档实测（自托管、Alerts、Metrics API v2、Masking、Python SDK 埋点五页）+ 本仓库现状实测。
> **上游**：`docs/校园RAG系统重构方案.md` §3.2.3（可观测性整章）、§3.2.3.5（必须 vs 加分）、§3.2.3.6（明确不做）、§3.2.4（并发与超时）、§4.4（仪表盘）、第 4433 行「数据不出网」。
>
> 本文只写「做什么、为什么这么定」。**凡与 §3.2.3 / §4.4 / 4433 行冲突处，本文明确标注并给出修订理由**——不默默改口径。§11 是主文档的修订清单。

---

## 0. 一句话

把可观测性从**自建 OTel 三件套**（Collector + Jaeger + Prometheus + 自研 trace_id + Prometheus 告警规则）
整体换成 **Langfuse 云平台**：用 Python SDK 在**四个既有收口点**手工埋点，trace_id 全面改用 Langfuse 的，
仪表盘运行指标面板改读 Langfuse Metrics API v2，告警改用 Langfuse Alerts。

**这不是推倒重来**——上一版把埋点收口在了四个地方（见 §1.3），所以换后端主要是「换实现」而非「重新铺」。

---

## 1. 现状（动手前必须知道的事）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 可观测性栈 = **三个容器**：`campus-rag-otel-collector`、`campus-rag-jaeger`、`campus-rag-prometheus` | `docker-compose.yml`，实测三容器 Up |
| 2 | **代码直接 import `opentelemetry.*`，但 `pyproject.toml` 里一个都没声明**——靠 `chromadb` / `langsmith` 的传递依赖带进来，实测版本 **1.42.1** | PyPI 元数据实测 + `pyproject.toml` 通读 |
| 3 | `trace_id` 落在**四处**：日志 JSON（`logging.py`）、`qa_logs.trace_id`（**已建索引**）、`ingestion_tasks.trace_id`、SSE 流全程 | `001_init.sql` L65-71/L152、`telemetry.py` 契约表 |
| 4 | 后台任务与 SSE **不靠 contextvars 隐式继承**，用 `reattach()` 显式续接 | `api/chat.py:34`、`services/document_service.py:151` |
| 5 | `telemetry.setup_tracing` / `metrics.setup_metrics` 同一开关：**没配 `OTEL_EXPORTER_OTLP_ENDPOINT` 就不挂 exporter** | `telemetry.py:65`、`metrics.py:37` |
| 6 | 业务指标（文档数/问答量/拒答率/降级次数）走 **PostgreSQL**，与运行指标是两套 | `metrics.py` 开头分工说明、§3.2.3 开头 |

### 1.1 ⚠️ 既有 Bug：告警规则引用了不存在的指标名（本次改造顺带消除）

`prometheus-rules.yml` 的 `RAGNodeP95High` 查 `spanmetrics_latency_bucket`，
但 collector 0.115.1 实际产出的是 **`spanmetrics_duration_milliseconds_bucket`**（按 OTel 语义约定改过名）。

- **实测**：`count(spanmetrics_latency_bucket)` → 空；Prometheus `/api/v1/rules` 报 `health=ok, state=inactive`
- **空结果不算错误**，所以是**静默失效**——规则永远不可能触发，且看不出异常
- `services/stats_service.py:189-195` **已经发现并修好了**这个问题（还留了注释），但**只改了那一个文件**，规则文件漏了
- `otel-collector-config.yaml` 的注释里也还写着旧名字，会误导后来人

> 这条本身就是换掉自建栈的动机之一：**自己维护 PromQL 与 collector 版本的耦合，代价比收益高**。

### 1.2 实测确认的 Langfuse 能力（不是推断）

| 项 | 结论 | 出处 |
|---|---|---|
| 云 vs 自托管 | 两套独立部署，**云上看不到自托管的数据**；但**同一份代码**（"Langfuse OSS and Enterprise use the same codebase as Langfuse Cloud"） | 自托管文档实测 |
| Alerts | **有**。按 `p95 latency`/cost/score 配阈值，支持 `>`/`≥`/`<`/`≤`/`=`/`≠`、告警+预警双阈值、时间窗口、无数据处理、重复通知；通知走 Slack / Webhook / GitHub Actions。云平台 **Hobby 档 2 条**、Core 20、Pro 50、Enterprise 100 | Alerts 文档实测 |
| Metrics API | **有 v2**：`GET /api/public/v2/metrics`，聚合 cost / token / latency / score，支持维度分组与趋势。默认 `row_limit=100`，最大 1000 | Metrics API 文档实测 |
| Masking | **有**。推荐钩子 `mask_otel_spans`（导出阶段改 OTel span 属性），另有 legacy `mask`（SDK 属性创建时同步跑）。官方定位原话：**"redact sensitive information before trace data leaves your application"**——**在数据离开应用之前**打码 | Masking 文档实测 |
| Python 兼容 | SDK `4.17.0`，`requires_python = <4.0,>=3.10` → **支持本项目的 3.13** | PyPI 元数据实测 |
| SDK 底层 | **Langfuse SDK 建在 OTel 之上**：依赖 `opentelemetry-api/sdk>=1.45.0` + `opentelemetry-exporter-otlp-proto-http` | PyPI 元数据实测 |

> ⚠️ **依赖版本冲突**：本项目当前 OTel 是 **1.42.1**，Langfuse 要求 **>=1.45.0**。装 Langfuse 会把 OTel 升上去。
> 顺带解决 §1 第 2 条那个「直接 import 却没声明」的隐患——OTel 从此由 Langfuse 显式带上。

### 1.3 埋点已收口在四处（这是本方案便宜的根本原因）

| # | 收口点 | 文件 | 覆盖范围 |
|---|---|---|---|
| 1 | 图节点 | `graph/builder.py` 的 `traced()` | **一个装饰器包住全部 10 个节点** |
| 2 | LLM 调用 | `core/llm.py` 的 `complete` / `complete_json` / `stream_raw` | 全部模型调用 |
| 3 | HTTP 入口 | `main.py` 的 `trace_middleware` | 全部请求 |
| 4 | 后台任务续接 | `chat.py:34`、`document_service.py:151` 的 `reattach()` | 两处 |

---

## 2. 决策清单

以下 **14 条**是设计过程中逐条确认的，**实现时不得自行改动**。前 7 条为负责人直接指定。

| # | 决策 | 备注 |
|---|---|---|
| 1 | **部署形态：Langfuse 云平台** | 负责人指定。代价见 §9.1（数据出校），缓解见 §7 |
| 2 | **接入方式：Python SDK 手工埋点**，不用自动集成（`opentelemetry-instrumentation-langchain` / OpenAI 集成） | 负责人指定「方案 A」。与既有「手工埋点、不用自动埋点」口径一致（`builder.py:99`） |
| 3 | **上传范围：脱敏上传** | 负责人指定。正文照传，标识符按规则打码，见 §7 |
| 4 | **trace_id 全面改用 Langfuse 的，删除 OTel 那一套** | 负责人指定。见 §6 |
| 5 | **仪表盘保留面板，数据源换成 Metrics API v2**；有价值的新指标一并加上 | 负责人指定。见 §8 |
| 6 | **删掉三个容器 + 三个配置文件** | 负责人指定。见 §9 |
| 7 | **主文档 `校园RAG系统重构方案.md` 一并修改** | 负责人指定。修订清单见 §11 |
| 8 | 图节点用 **`@observe(as_type="span")`**，不用 `opentelemetry-instrumentation-langchain` | 决策 2 的落地形式 |
| 9 | LLM 调用用 **`as_type="generation"`** 手工建观测，**手填 usage** | 本项目 LLM 客户端是**手写 httpx**（非 OpenAI SDK），自动抓 usage 不适用，必须手填（见 §5.2） |
| 10 | 后台任务用 **`trace_context={"trace_id":..., "parent_span_id":...}`** 续接，替代 `reattach()` | 官方为此场景提供的正是这个参数（§5.4） |
| 11 | **业务指标（PG 侧）一律不动**：`qa_logs` / `degradation_events` / `stats/overview` 等照旧 | 它们不是可观测性栈，是业务数据（§1 第 6 条） |
| 12 | **日志照旧走本地 JSON 文件**，不接 Langfuse | Langfuse 不做日志存储；`RotatingFileHandler` 沿用（§3.2.3.6「不引日志检索平台」） |
| 13 | 脱敏用 **`mask_otel_spans`** 钩子（不是 `mask`） | 官方说明：新接入首选 `mask_otel_spans`；且它在**导出阶段**跑，覆盖 SDK 自己产生的 span 属性 |
| 14 | 保留 **`traceparent` 响应头**，但值改为 Langfuse 的 trace_id | 前端与用户报障链路不断（§3.2.3.1「返回前端」那条契约） |

---

## 3. 目标架构与数据流

```
FastAPI + LangGraph
    │
    │  Langfuse Python SDK（手工埋点，四个收口点）
    ▼
┌───────────────────────  Langfuse 云  ───────────────────────┐
│                                                             │
│   Trace 树（图节点 span + LLM generation）   ← 取代 Jaeger   │
│   Alerts（p95 / 成本 / 错误阈值）            ← 取代 rules.yml │
│   Metrics API v2                             ← 取代 PromQL   │
└──────────────────────────┬──────────────────────────────────┘
                           │  httpx（后端调用）
                           ▼
              后端 `/api/admin/stats/retrieval`
                           ▼
                自研管理仪表盘「运行指标」面板（保留）
                           │  点异常 → 深链
                           ▼
                    Langfuse UI（看单条 trace）

        旁路（不变）：
        日志 → logs/app.jsonl（本地 JSON，带 trace_id）
        业务指标 → PostgreSQL（qa_logs / degradation_events）
```

**分工没变**：Langfuse 答「什么时候、哪个环节变慢了」，Langfuse UI 答「那一次具体慢在哪一步」。
变的是**两者共用同一份数据**（以前是 Prometheus 与 Jaeger 共用 OTel 数据，现在是 Langfuse 一家）。

---

## 4. 模块改动总览

| 动作 | 对象 |
|---|---|
| **删** | `core/telemetry.py`（整文件，能力由 Langfuse 提供） |
| **删** | `core/metrics.py`（`llm_tokens_total` 由 generation 的 usage 取代） |
| **删** | `otel-collector-config.yaml`、`prometheus.yml`、`prometheus-rules.yml` |
| **删** | `docker-compose.yml` 里 jaeger / otel-collector / prometheus 三个 service 与 `promdata` volume |
| **删** | `scripts/start.sh` 里起三容器与等 Collector 就绪的段落 |
| **删** | `tests/unit/test_span_redaction.py`（改写成 Langfuse 版，见 §12） |
| **新增** | `core/observability.py` —— Langfuse 客户端单例 + 脱敏钩子 + trace_id 取值，**统一出口** |
| **改** | `graph/builder.py` —— `traced()` 内部换实现，**签名与调用点不变** |
| **改** | `core/llm.py` —— 三个函数包 generation；`_current_node()` 换数据源 |
| **改** | `main.py` —— 中间件换实现；`lifespan` 里换初始化与 shutdown |
| **改** | `core/logging.py` —— `JsonFormatter` 的 trace_id 取值换数据源 |
| **改** | `api/chat.py`、`services/document_service.py` —— `reattach()` 调用换成 Langfuse 版 |
| **改** | `services/stats_service.py` —— `_PROM_QUERIES` → Metrics API v2 查询；`_prom_rules()` → Langfuse Alerts 状态 |
| **改** | `pyproject.toml` —— 加 `langfuse` |
| **改** | `.env` / `.env.example` —— 加 `LANGFUSE_*` |
| **改** | 前端 `AdminDashboard.tsx` —— 面板适配新字段（§8.2） |
| **改** | 主文档 —— 见 §11 |

---

## 5. 埋点设计（四个收口点）

统一出口 `core/observability.py`，对外只暴露四样东西，其余模块**不直接 import langfuse**：

```python
def setup(service_name: str) -> None: ...        # 初始化客户端（lifespan 里调）
def shutdown() -> None: ...                       # flush + shutdown（lifespan 退出调）
def current_trace_id() -> str: ...                # 32 位十六进制；无则全 0
def attach_trace(trace_id: str, span_id: str = "") -> ContextManager: ...  # 替代 reattach
def observe_node(name: str) -> Callable: ...      # 替代 traced 的装饰器工厂
def record_generation(...) -> ContextManager: ... # 包 LLM 调用
```

### 5.1 图节点：`graph/builder.py` 的 `traced()`

**调用点一行不改**（10 个节点仍写 `graph.add_node("resolve", traced("resolve", resolve_node))`），只换内部实现：

| 现在 | 改成 |
|---|---|
| `tracer.start_as_current_span(f"node.{name}", record_exception=False, set_status_on_exception=False)` | `langfuse.start_as_current_observation(as_type="span", name=f"node.{name}")` |
| `span.set_attribute("rag.duration_ms", ...)` | `span.update(metadata={...})` |
| 异常只记类型的护栏 | **仍然需要**——见下 |

⚠️ **`record_exception=False` 那条护栏不能丢**。Langfuse 的 `@observe` 默认**自动捕获输入输出与异常**
（官方原话："automatically capture inputs, outputs, timings, and errors"）。
而 `builder.py:90` 已经写明：LLM 异常的 message 可能回显 prompt。

**做法**：节点观测一律 **`capture_input=False, capture_output=False`**，
异常只写 `metadata={"rag.error": type(e).__name__}`，**不把异常对象交给 SDK**。
§12 的测试就是这条承诺的凭据。

### 5.2 LLM 调用：`core/llm.py`

`complete` / `complete_json` / `stream_raw` 三个函数各包一层 generation 观测。

⚠️ **必须手填 usage**：本项目 LLM 客户端是**手写 httpx**（不是 OpenAI SDK），
Langfuse 的自动集成抓不到；但 `llm.py` 里**本来就已经解析了 usage**
（`_record_usage()`，`complete` 在 L116、`stream_raw` 在 L199 先取 usage 再判 choices）。
把那份 usage 从「记进 `llm_tokens_total`」改成「写进 generation 观测」即可——**不用新增解析逻辑**。

| 字段 | 值 | 说明 |
|---|---|---|
| `model` | `_model()` | 供 Metrics API 按模型分组、供成本计算 |
| `usage_details` | `{"input": ptok, "output": ctok}` | 输入/输出分开，**token 用量面板因此能拆开显示**（现在只有一个合计） |
| `input` | `messages` | ← **要过脱敏**，见 §7 |
| `output` | 返回值 | ← **要过脱敏**，见 §7 |

⚠️ `stream_raw` 是**流式**：开始与结束不连续。用 `start_observation()` 手工管理生命周期
（官方说明：适用于"start and end are determined by non-contiguous events"），
在 `finally` 里 `.end()`——**漏了 `.end()` 观测会根本不出现**（官方明写 "Manual Ending Required"）。

### 5.3 HTTP 入口：`main.py` 中间件

| 现在 | 改成 |
|---|---|
| `telemetry.extract_context(carrier)` + `start_as_current_span` | `langfuse.start_as_current_observation(as_type="span", name=f"{method} {path}", trace_context=...)` |
| `span.set_attribute("http.status_code", ...)` | `span.update(metadata={"http.status_code": ...})` |
| 响应头 `traceparent` / `x-trace-id` | **保留**，值改从 `observability.current_trace_id()` 取（决策 14） |

⚠️ **中间件里 `get_current_trace_id()` 是否可靠，需实测**（§12.1 第 1 条）——
Starlette 的中间件与 Langfuse 的 OTel 上下文是否在同一 context 上，文档没写，**必须跑一次真实请求验证**。

### 5.4 后台任务：`reattach()` → `attach_trace()`

这是**唯一机制上等价**的一处，官方为此提供了专门的参数：

```python
# 现在
with reattach(trace_id, span_id): ...
# 改成
with langfuse.start_as_current_observation(
    as_type="span", name="ingestion", 
    trace_context={"trace_id": trace_id, "parent_span_id": span_id},
): ...
```

调用点仍是那两处（`chat.py:34`、`document_service.py:151`），
**`ingestion_tasks.trace_id` 落库照旧**——「后台任务不能靠 contextvars 隐式继承」这条教训不变（§3.2.3.1）。

---

## 6. trace_id 统一方案（决策 4）

**好消息**：Langfuse 的 trace ID 是 **32 位十六进制（16 字节）**，
与现在 OTel 的 trace_id **格式完全相同**（官方原话："Trace IDs ... 32-character lowercase hex strings"）。

**这意味着以下全部不用改**：

| 项 | 为什么不用改 |
|---|---|
| `qa_logs.trace_id TEXT` 及索引 | 格式相同，TEXT 存得下 |
| `ingestion_tasks.trace_id TEXT` | 同上 |
| 日志 JSON 的 `trace_id` / `span_id` 字段 | 格式相同，`JsonFormatter` 只换取值来源 |
| 全 0 兜底（`"0"*32`） | 保留同样语义：无有效 trace 时的合法占位 |

**要改的**：

| 文件 | 改动 |
|---|---|
| `core/telemetry.py` | **删除**，能力并入 `core/observability.py` |
| `core/logging.py` | `JsonFormatter.format()` 里那段 `from opentelemetry import trace` 换成 `observability.current_trace_id()` |
| `core/llm.py` 的 `_current_node()` | 现在靠**读 OTel 当前 span 名**反推节点（L60-68）。换成读 Langfuse 当前观测名——同样从 `node.<名字>` 反推。⚠️ **SDK 只保证有 `get_current_observation_id()`，没写有「取当前观测名」的方法**，故这条列为动手前必验项（§12.1 第 4 条）；取不到就退回 contextvar 方案（见下） |
| `pyproject.toml` | 删掉对 `opentelemetry-*` 的**直接 import 依赖**（代码不再直接用；SDK 由 langfuse 带上） |

⚠️ **`logging.py` 有个约束**：现在那段 OTel 取值包在 `try/except` 里，
注释写着「日志本身不能因为埋点故障而挂掉」（L73）。**换实现后这个 `try/except` 必须保留**。

---

## 7. 脱敏策略（决策 3）

### 7.1 边界的变化（必须写清楚）

| | 改造前 | 改造后 |
|---|---|---|
| 提问原文 | 只在本地 PG（`qa_logs`） | **上传到 Langfuse 云** |
| 检索片段 | 只在本地 PG | **上传到 Langfuse 云** |
| 答案正文 | 只在本地 PG | **上传到 Langfuse 云** |
| 学号 / 姓名 / 手机号 | 本地（原始） | **本地就打码**，上传的是打码后文本 |

**一句话**：从「数据不出网」变成「**正文出网、标识符不出网**」。
主文档第 4433 行那条性质**必须改**（修订项见 §11）。

### 7.2 钩子：`mask_otel_spans`

官方推荐的新钩子，**在导出阶段、数据离开应用之前**运行
（原话："redact sensitive information before trace data leaves your application"）。
接收一批 span 的快照，返回稀疏补丁（`delete_attributes` / `set_attributes`）。

与现有 `logging.py` 的 `_scrub()` 的**分工**：

| | `logging.py`.`_scrub`（保留） | `observability` 的 mask（新增） |
|---|---|---|
| 管什么 | **本地日志** | **上传 Langfuse 的数据** |
| 机制 | 敏感**键名**黑名单 → 整值替换 | 敏感**模式**正则 → 文本内打码 |
| 为什么两套 | 日志是键值结构，键名可枚举 | Langfuse 的 input/output 是**整段文本**，学号藏在句子中间，键名法抓不到 |

⚠️ **两套都要留**。只留一套会漏：日志那套管不住 span 里的整段 prompt，Langfuse 那套管不住本地日志。

### 7.3 打码规则（配置化，不写死在代码里）

沿用 `app.yaml` + `security.yaml` 的既有做法（`app.yaml` 顶部写明「规则词表必须配置化」）：

```yaml
observability:
  masking:
    enabled: true
    # 命中即整段替换为 [REDACTED:<类型>]，保留「这里原本有个东西」的信息
    patterns:
      - name: student_id     # 学号：10 位纯数字
        regex: '\b\d{10}\b'
      - name: phone          # 手机号
        regex: '\b1[3-9]\d{9}\b'
      - name: id_card        # 身份证
        regex: '\b\d{17}[\dXx]\b'
      - name: email
        regex: '\b[\w.+-]+@[\w-]+\.[\w.]+\b'
```

⚠️ **配置化的理由与 `app.yaml` 里那条一致**：规则会随场景变（比如以后加学号前缀），
写死在代码里意味着每次都要改代码 + 重新部署 + 重跑测试。

### 7.4 验收（写进测试，不靠人工看 UI）

沿用 `test_span_redaction.py` 的思路：**构造一条必然含敏感标记的调用，断言它在导出前已被打码**。
人工去 Langfuse UI 翻是**一次性**的；测试是**每次都跑**的（原测试的这句理由依然成立）。

---

## 8. 指标与告警（决策 5）

### 8.1 `stats_service.py` 的改造

| 现在 | 改成 |
|---|---|
| `_PROM_QUERIES` 五条 PromQL，打 Prometheus | 五条 **Metrics API v2** 查询，打 Langfuse（`httpx` 调 `GET /api/public/v2/metrics`） |
| `_prom_rules()` 读 `/api/v1/rules` | 读 **Langfuse Alerts 的当前状态** |
| `PROMETHEUS_URL` 未配 → `available: false` | 换成 `LANGFUSE_*` 未配 → `available: false`（**语义与降级路径完全保留**） |

⚠️ **降级契约不变**：§4.4 写的「Prometheus 不可用时运行指标区块显示暂不可用、业务指标照常渲染」
**原样保留**，只是把「Prometheus 不可用」换成「Langfuse 不可用」。api 层仍返 **HTTP 200**（M4-D5）。

⚠️ **`or vector(0)` 那条修补要移植**：`stats_service.py:203-205` 记着一个实测踩过的坑——
一次 5xx 都没有时序列不存在 → 整条表达式无结果 → 面板显示「—」看起来像没接上。
换成 Metrics API 后，**同样要处理「结果为空」与「结果真的是 0」的区别**。

⚠️ **`token_usage` 用累计值不用 rate** 的教训同样移植：`stats_service.py:211-213` 写明
「问答是稀疏事件，窗口两端数值常常相同，rate 会算成 0（实测踩过 —— 指标明明有 331，rate 报 0）」。

### 8.2 新增指标（决策 5 后半句：「有价值的就加上」）

换成 Langfuse 后**白捡**的、建议加的：

| 新指标 | 为什么值得 | 数据来源 |
|---|---|---|
| **LLM 成本** | 现在**完全没有**。Langfuse 按模型自动算，是它最直接的增值 | Metrics API 的 cost |
| **token 拆输入/输出** | 现在只有一个合计（`sum(llm_tokens_total)`）。generation 的 usage 天然分开 | `usage_details` 按 `input`/`output` 分组 |
| **按节点的耗时 p95** | 现在只有全局 p50/p95。告警规则本来就想按节点分组（`rag.node`），但自建那套没做成面板 | Metrics API 按观测名分组 |

> 前两项是**净增能力**；第三项是把 §3.2.3.3 仪表盘表里那条「节点耗时分位数（**按节点分组**）」
> **真正做出来**——自建栈里它一直只存在于告警规则里，面板从未实现。

### 8.3 告警：`prometheus-rules.yml` → Langfuse Alerts

三条现有规则逐一映射：

| 现规则 | Langfuse Alert 配法 | 备注 |
|---|---|---|
| `RAGNodeP95High`（节点 p95 > 10s） | 数据源 Observations，指标 p95 latency，窗口 5m，阈值 10 | **这条现在是坏的**（§1.1），换过去就修好了 |
| `RAGHttp5xxRate`（5xx > 5%） | 数据源 Observations，按 status 过滤计数，窗口 5m，阈值 5% | |
| `RAGSpanErrorRateHigh`（错误率 > 5%） | 同上 | 原注释写明「本项目当前不标 span 状态，平时恒为 0」——**这条要么真去标状态，要么删掉** |

⚠️ **云平台 Hobby 档只有 2 条告警额度**。三条规则**放不下**，需要取舍——见 §13.1 未决项。

---

## 9. 删除清单（决策 6）

### 9.1 容器与配置

| 删 | 位置 |
|---|---|
| `jaeger` service + 端口 16686 | `docker-compose.yml` |
| `otel-collector` service + 端口 4317/4318 | 同上 |
| `prometheus` service + 端口 9090 | 同上 |
| `promdata` volume | 同上 |
| `otel-collector-config.yaml` | 仓库根 |
| `prometheus.yml` | 仓库根 |
| `prometheus-rules.yml` | 仓库根 |
| `scripts/start.sh` 里起三容器 + 等 Collector 就绪的段落 | `scripts/start.sh:23-44` |

> ⚠️ `start.sh` 那个「等 Collector 就绪」的循环本来就有名不副实的问题
> （`docker exec ... --version` 只证明容器活着，不检测 OTLP 端口），一并删掉正好。

### 9.2 代码

| 删 | 说明 |
|---|---|
| `core/telemetry.py` | 整文件。`reattach` / `current_trace_id` / `inject_context` 的能力并入 `observability.py` |
| `core/metrics.py` | `llm_tokens_total` 由 generation 的 usage 取代（决策 9） |

### 9.3 ⚠️ 删除前必做

`.env` 与 `.env.example` 里的 `OTEL_EXPORTER_OTLP_ENDPOINT` **删掉之前**先确认：
它现在被 `telemetry.py:65` 与 `metrics.py:37` 读取，是「挂不挂 exporter」的总开关。
换 Langfuse 后这个开关由 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 是否存在接管——
**测试与 CLI 环境不配这两个变量**，行为与现在一致（不发外网）。这条要写进 `.env.example` 的注释。

---

## 10. 依赖与配置

### 10.1 `pyproject.toml`

```toml
dependencies = [
    ...
    "langfuse>=4.17.0",     # 新增。支持 Python 3.13（requires_python <4.0,>=3.10）
]
```

⚠️ **会连带升级 OTel**：当前装的是 1.42.1（chromadb 带的），Langfuse 要求 `opentelemetry-sdk>=1.45.0`。
升级后要**重跑一遍全量测试**确认 chromadb 不受影响。

### 10.2 `.env`

```bash
# 可观测性（Langfuse 云）
# ⚠️ 两个都不配 = 完全不发外网（跑测试、跑 CLI 时就是这种状态）
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com    # ⚠️ 必填，见下
```

⚠️⚠️ **`LANGFUSE_HOST` 必须显式写**：Langfuse SDK 的 host 有**默认值**，
配错了 key 但没配 host 时，**会静默往 `cloud.langfuse.com` 发**，本地还不报错。
这是公开记录过的坑。写死它，并且**在 §12 的测试里断言 host 可配置**。

### 10.3 密钥管理

沿用 `security.yaml` 的既有做法（**只存环境变量名，绝不存值**）：

```yaml
observability:
  langfuse_public_key: LANGFUSE_PUBLIC_KEY
  langfuse_secret_key: LANGFUSE_SECRET_KEY
  langfuse_host: LANGFUSE_HOST
```

---

## 11. 主文档修订清单（决策 7）

`docs/校园RAG系统重构方案.md` 需改动的位置，**逐处列出，不遗漏**：

| 行/节 | 现在写的 | 要改成 |
|---|---|---|
| **§3.2.3 整章** | 以 OTel 三支柱为骨架，含「为什么用 OTel 而不是自研」四条理由 | 重写为 Langfuse 方案；「为什么用 OTel」那四条**保留但降级为背景**（说明一度选它的理由），补「为什么最终换 Langfuse」 |
| §3.2.3.1 `trace_id` 契约表 | 「生成位置：HTTP 入口中间件」「传播格式：W3C Trace Context」 | 生成位置改为 Langfuse SDK；传播格式保留 W3C（Langfuse 兼容）；**后台任务那条不变**（仍然要显式传递） |
| §3.2.3.2 日志 schema | 字段表保留 | **基本不动**（日志照旧，决策 12）。但「span 属性必须同级脱敏」那段要改写为脱敏钩子 |
| §3.2.3.3 指标栈与数据流图 | OTel Collector + spanmetrics + Prometheus + Jaeger 四层图 | 换成 §3 的目标架构图 |
| §3.2.3.4 接入范围与采样 | 表格写 `opentelemetry-instrumentation-langchain` | 改为手工埋点；采样策略改为 Langfuse 侧配置 |
| §3.2.3.5 必须 vs 加分 | ①~⑦ 七项 | 重列（Langfuse 的能力边界与原来不同，例如「告警规则」从加分项变成平台自带） |
| **§3.2.3.6「明确不做」** | **「商业 APM（Datadog / New Relic 等）：内网部署、无预算、数据不应出网」** | ⚠️ **必须改**。这条与决策 1 直接冲突。建议改为「**通用商业 APM 不做**（理由不变）；Langfuse 是 LLM 专用，且已用脱敏钩子控制出网内容，见 §7」 |
| **第 4433 行** | **「数据不出网 ✅ 每段检索文本只在本地处理」** | ⚠️ **必须改**。改为「**标识符不出网**：正文经脱敏钩子处理后才上传 Langfuse，学号/手机号/身份证在本地打码」 |
| §4.4 仪表盘表 | 运行指标五行的指标名写的是 PromQL 形态 | 改成 Metrics API 的查询形态；补 §8.2 的三个新指标 |
| §3.2.3.3「不引入 Alertmanager」段落 | 理由「无人值班、无接收方」 | **保留理由**，补一句：改用 Langfuse Alerts 后，通知渠道（Slack/Webhook）仍是可选而非必配 |
| 附录 G / 部署章节 | 「M5 会追加三个可观测容器」 | 改为「M5 接入 Langfuse 云，不再自建容器」 |
| 分支策略相关表述 | 若提到三容器 | 同步 |

⚠️ **改动方向的一致性**：§3.2.3.6 与第 4433 行这两处**是同一件事的两面**（一个说「不做」，一个说「不出网」），
**必须一起改**，只改一处会让文档自相矛盾。

---

## 12. 测试

### 12.1 必须先做的实测（否则不要动手写实现）

按「先实测，再下结论」，**四条都要先跑通**：

| # | 要验证的 | 为什么必须先验 |
|---|---|---|
| 1 | **中间件里 `get_current_trace_id()` 拿得到值**，且与 Langfuse UI 里看到的一致 | Starlette 中间件与 Langfuse 的 OTel 上下文是否同一 context，**文档没写**。拿不到就要改用别的方式注入 |
| 2 | **`trace_context` 能把后台任务挂到原 trace 上** | 这是 `reattach()` 的替代品，是「按 trace_id 还原双链路」验收的落点 |
| 3 | **`mask_otel_spans` 真的在导出前跑**（构造含标记的文本，确认 Langfuse 收到的是打码后的） | 官方说在导出阶段跑，但**上一版就栽在「注释声称安全 ≠ 实际安全」**（`builder.py:93-97`）。不许只看文档 |
| 4 | **能从 SDK 取到「当前观测名」**（供 `_current_node()` 反推节点） | SDK 文档只保证 `get_current_observation_id()`（返回 ID 不返回名字）。取不到就必须换方案，而它影响 `llm_tokens_total` 的按节点分组 |

> **第 4 条的兜底方案**（取不到名字时）：
> `observability.observe_node()` 在进入时把一个 `ContextVar[str]` 设成节点名，`_current_node()` 读它。
> 这就是**回到 contextvars**——而 §3.2.3.1 明确警告过后台任务不能靠 contextvars 隐式继承。
> ⚠️ 但**此处不矛盾**：节点执行是**同步的请求内调用链**，不是跨 asyncio 任务的长任务，
> contextvars 在同步调用链上是可靠的。**跨任务的那两处仍然走 `trace_context`**（§5.4）。

### 12.2 新增测试

| 测试 | 断言 |
|---|---|
| `test_langfuse_masking.py` | 构造含学号/手机号的 prompt，断言**导出前**已打码（沿用 `test_span_redaction.py` 的正控思路：既要断言敏感串不在，也要断言该有的字段在） |
| `test_langfuse_no_content_in_span.py` | 节点观测**不含**输入输出（`capture_input/output=False` 的凭据） |
| `test_langfuse_trace_id.py` | 同一次请求的日志 trace_id == 响应头 trace_id == Langfuse 的 trace_id |
| `test_langfuse_host_config.py` | 断言 host 取自配置，且未配置时**不往云端发** |

### 12.3 回归（必须仍通过）

| 测试 | 为什么 |
|---|---|
| `test_graph_trace.py` | 图执行轨迹的既有断言 |
| `test_stage_events.py` | SSE 事件（trace 上下文跨流） |
| `test_purge_logging.py` | 日志脱敏 |
| **全量** | ⚠️ OTel 从 1.42.1 升到 >=1.45.0，**chromadb 依赖它**，必须全量回归 |

### 12.4 验收标准（人工端到端，负责人指定）

自动化测试（§12.2）保证**每一段**是对的；本节保证**接起来**是对的。
两条**都必须在真实环境跑，不许用 mock**——mock 掉的正是最容易出问题的接缝。

**前置条件**：需要可用的 LLM API Key（会真实消耗 token）；精排模型预热实测约 29 秒，
预热没完就提问会撞在加载锁上等（`main.py:46-65`），验收前先等日志里出现 `rerank.preloaded`。

#### 验收 A：端到端 query → Langfuse 官网上看到完整追踪链路

**怎么跑**：`bash scripts/start.sh` → 登录 → 提一个**知识型问题**
（必须触发完整图；不能是寒暄——那会被 `route` 分流走，验不到链路）→ 等流式回答结束。

**逐条核对，全部满足才算通过**：

| # | 核对项 | 判据 |
|---|---|---|
| 1 | Langfuse 官网能查到这一次 | 按时间范围 + service 过滤能定位到那一条 trace |
| 2 | **是一棵完整的树，不是散点** | HTTP span 为根 → 图节点 span 为子 → LLM generation 为孙，**父子关系正确** |
| 3 | 图节点**齐全** | 实际走过的节点都在（`node.resolve` / `route` / `retrieve` / `rerank` / `generate` / `cite`），不重不漏 |
| 4 | 每个 LLM 调用是一条 **generation**，带 token | input / output token 都有值，**不是 0** |
| 5 | **一次 SSE 流全程同一个 trace_id** | 流内事件不分裂成多条 trace（§3.2.3.1 契约） |
| 6 | **四处 trace_id 一致** | 响应头 `x-trace-id` == 日志 `trace_id` == `qa_logs.trace_id` == Langfuse UI 显示的 |
| 7 | **脱敏生效** | 拿一条**含测试学号**的提问去跑，Langfuse 里看到的 prompt / 答案中该学号**已打码** |
| 8 | **后台任务挂在原 trace 下** | 上传文档触发解析，解析的 span 挂在**那次上传请求**的 trace 下，不是新开一条（`reattach` 的落点） |

> ⚠️ **第 7 条是硬项**，也是唯一能证明「脱敏不是在骗自己」的方式。
> 第 8 条单独跑一次——它和 1~7 走的不是同一条路径（§5.4）。

#### 验收 B：仪表盘端到端功能检查

**怎么跑**：起服务 → admin 登录 → 打开仪表盘。

| # | 面板 | 判据 |
|---|---|---|
| 1 | 核心指标卡 | 文档数 / chunk 数 / 问答量 / 拒答率 有值（业务侧，应不受影响） |
| 2 | 运行指标 —— 耗时 | p50 / p95 有值，**不是 NaN、不是「—」** |
| 3 | 运行指标 —— 错误率 | 有值 |
| 4 | 运行指标 —— token 用量 | 有值，且**输入 / 输出能拆开**（§8.2 新增） |
| 5 | 运行指标 —— LLM 成本 | 有值（§8.2 新增） |
| 6 | 告警状态卡 | 显示规则数与 firing 状态，不报错 |
| 7 | 业务指标区块 | 照常渲染（趋势 / 拒答分布 / 高频问题 / 降级次数） |

**降级路径必须单独验一次**：

| 8 | 把 `LANGFUSE_*` 配错（或断网）→ 重开仪表盘 | ① 运行指标区块显示「运行指标暂不可用」② **业务指标区块照常渲染** ③ 接口返 **HTTP 200 不是 500** ④ 提问主链路不受影响 |

> 第 8 条是 §4.4「Prometheus 不可用时的降级」契约的移植版，**必须实测**——
> 降级路径是典型的「平时看不出问题、出事才发现根本没接」的地方。

---

## 13. 已知风险与不做的事

### 13.1 未决项（实施前需负责人拍板）

| # | 问题 | 影响 |
|---|---|---|
| 1 | **云平台 Hobby 档只有 2 条告警额度**，现有 3 条规则放不下 | 要么删一条（`RAGSpanErrorRateHigh` 本来就恒为 0），要么升级档位 |
| 2 | **Langfuse 云的保留期与项目要求是否匹配** | §3.2.3.6 原写「Prometheus 保留 15 天」。Langfuse 云各档保留期不同，需按实际档位核对 |

### 13.2 明确不做

| 不做 | 原因 |
|---|---|
| 自托管 Langfuse | 负责人选云平台（决策 1）。自托管需 6 个容器（web/worker/PG/ClickHouse/Redis/MinIO），比现在**更重** |
| 本地兜底日志追踪（Loki/ES） | §3.2.3.6 既定的「不引日志检索平台」不变 |
| 自动集成（LangChain/OpenAI instrumentation） | 决策 2。且本项目 LLM 客户端是手写 httpx，自动集成本来也抓不到 |
| 业务指标迁到 Langfuse | 决策 11。它们是业务数据，不是运行指标 |
| 保留任何 OTel 直接依赖 | 决策 4。代码不再直接 import `opentelemetry.*` |

### 13.3 风险

| 风险 | 缓解 |
|---|---|
| **数据出校**（决策 1 的固有代价） | §7 脱敏钩子 + §12.1 第 3 条实测 + §12.2 测试。**但这是缓解不是消除**——正文仍然出网 |
| OTel 1.42.1 → >=1.45.0 影响 chromadb | §12.3 全量回归 |
| SDK 埋点漏 `.end()` 导致观测消失 | 官方明写 "Manual Ending Required"。流式那条用 `try/finally`，并写测试 |
| Langfuse 云不可用时拖慢主链路 | 官方 SDK 异步缓冲；但**要走 §8.1 的降级契约**（不可用 → `available: false`，业务不受影响） |
| 主文档改动遗漏导致自相矛盾 | §11 逐行清单；**§3.2.3.6 与 4433 行必须同时改** |

---

## 附录：决策溯源

| 决策 | 来源 | 落到哪 |
|---|---|---|
| 1 云平台 | 负责人 2026-10-11 指定 | §3、§13.1、§11（文档冲突） |
| 2 方案 A（SDK 手工埋点） | 负责人指定「方案 a」 | §5 |
| 3 脱敏上传 | 负责人从三选一里选定 | §7、§11（4433 行） |
| 4 trace_id 全换 Langfuse | 负责人指定 | §6 |
| 5 保留面板改读 Metrics API v2 + 加新指标 | 负责人指定 | §8 |
| 6 删三容器三配置 | 负责人指定 | §9 |
| 7 一并改主文档 | 负责人指定 | §11 |
| 8~14 | 由决策 1~7 推导，实现时不得自行改动 | §2 |
| 验收 A / B（两条端到端） | 负责人 2026-10-11 追加指定 | §12.4 |

**事实来源**：Langfuse 官方文档五页实测（自托管 / Alerts / Metrics API v2 / Masking / Python SDK 埋点）+
PyPI 元数据实测（`langfuse 4.17.0`）+ 本仓库代码与文档通读 + 本地三容器的 Prometheus/Jaeger 实查询。
**凡标注「实测」的均非推断**；凡未实测的一律在 §12.1 列为动手前必须先验的项。
