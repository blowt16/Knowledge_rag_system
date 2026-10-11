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

> ✅ **已实测**：带这两个参数的 `@observe` 包住 async 函数后，**正文标记未出现在发送字节里**
> （§12.1 上表 #5、#6，用本地捕获服务器验的，不是看文档）。
> §12.2 的测试把这条锁死。

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

⚠️ **但「全 0 兜底」不是白捡的**：实测**未配置密钥时 `get_current_trace_id()` 返回 `None`**。
`observability.current_trace_id()` **必须显式把它转成 `"0"*32`**，否则日志字段变成 `null`，
破坏既有契约（前端错误提示、日志检索都按「合法字符串」用它）。写法见 §12.1 修正 2。

**要改的**：

| 文件 | 改动 |
|---|---|
| `core/telemetry.py` | **删除**，能力并入 `core/observability.py` |
| `core/logging.py` | `JsonFormatter.format()` 里那段 `from opentelemetry import trace` 换成 `observability.current_trace_id()` |
| `core/llm.py` 的 `_current_node()` | 现在靠**读 OTel 当前 span 名**反推节点（L60-68）。⚠️ **实测确认 Langfuse 不提供「当前观测名」（`obs.name` 不存在）**，故改为读 `observability` 自记的 contextvar——见 §12.1 修正 1 |
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
        regex: "(?<!\\d)\\d{10}(?!\\d)"
      - name: phone          # 手机号（容忍 - 与空格分隔）
        regex: "(?<!\\d)1[3-9]\\d[\\s-]?\\d{4}[\\s-]?\\d{4}(?!\\d)"
      - name: id_card        # 身份证（18 位 + 15 位旧式）
        regex: "(?<!\\d)(?:\\d{17}[\\dXx]|\\d{15})(?!\\d)"
      - name: email
        regex: "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"
```

⚠️⚠️ **绝不要用 `\b` 做边界（本项目踩过，是最严重的一次漏打码）**：
Python `re` 的 `\w` **包含汉字**，所以**中文字符与数字之间不存在 `\b`**。
实测：

| 写法 | 用 `\b` 时 |
|---|---|
| `学号 2023123456 的申请`（带空格） | 打码 ✅ |
| **`我的学号是2023123456`**（用户最自然的写法） | **完全不打码** ❌ |
| **`联系方式13800138000`** | **完全不打码** ❌ |

而带空格的写法恰好是**测试里**的写法 —— 于是"测试全绿"与"生产漏打码"可以同时成立。
**改法**：数字规则一律用 `(?<!\d)` / `(?!\d)` 做边界；邮箱规则**完全不用 `\w`**，
改用显式 ASCII 字符类 —— 否则 `[\w.+-]+` 会把邮箱前面的整段中文一起吞掉。

⚠️ **正则一律用双引号写**（与 `app.yaml` 里 `rules.doc_number_pattern` 同款）。
**YAML 单引号里反斜杠是字面量** —— 写成 `'\\d'` 会变成「匹配一个反斜杠 + d」，
规则**静默失效**且测试之外看不出来。施工时已实测确认（Task 3）。

⚠️ **配置化的理由与 `app.yaml` 里那条一致**：规则会随场景变（比如以后加学号前缀），
写死在代码里意味着每次都要改代码 + 重新部署 + 重跑测试。

#### ⚠️ 已知不覆盖（明写在案，不是遗漏）

| 不覆盖 | 为什么不做 |
|---|---|
| **姓名** | 中文姓名是 2–4 字的极常见子串，正则会把大量正常正文一起打掉 —— 那等于毁掉观测本身（这是引入 Langfuse 的全部理由）。名字的脱敏应在数据源头解决，不该由正则兜 |
| **非 10 位学号** | 位数是**配置值**：真实格式由项目负责人按本校实际填。不做成 `\d{8,12}` 是因为那会连年份、金额、计数一起吃掉 |
| **其它证件类型** | 规则表是配置项，按需加行即可 —— 这正是配置化而非写死的意义 |

> 这三条由 Task 3 的审查提出（审查员指出「只覆盖规范格式」且**漏打码方向未被测试**）。
> 裁定：**能廉价加宽的两条已加宽**（手机号容忍分隔符、身份证兼容 15 位），
> **靠正则做不到的明确列为不覆盖**，不硬凑。

### 7.4 验收（写进测试，不靠人工看 UI）

沿用 `test_span_redaction.py` 的思路：**构造一条必然含敏感标记的调用，断言它在导出前已被打码**。
人工去 Langfuse UI 翻是**一次性**的；测试是**每次都跑**的（原测试的这句理由依然成立）。

---

## 8. 指标与告警（决策 5）

### 8.1 `stats_service.py` 的改造

> ✅ **本节形状已对着真实项目实测**（2026-10-11，见 §12.4 验收 D），不是文档推断。

**实测确认的调用形状**：

```
GET /api/public/v2/metrics?query=<JSON 字符串>        ← 是【一个 query 参数】，不是散装参数
Authorization: Basic <public_key>:<secret_key>

query = {
  "view": "observations",
  "metrics": [{"measure": "latency", "aggregation": "p95"}],
  "dimensions": [{"field": "name"}],          # 可选，按维度分组
  "fromTimestamp": "2026-10-01T00:00:00Z",    # ⚠️ 必填，无默认值
  "toTimestamp":   "2026-10-12T00:00:00Z"     # ⚠️ 必填
}
→ {"data": [{"name": "node.retrieve", "p95_latency": 1234}]}
```

返回字段名是 **`<聚合>_<指标>` 拼接**（实测：`count_count`、`p95_latency`、`sum_totalCost`、`sum_totalTokens`）。

| 现在 | 改成 |
|---|---|
| `_PROM_QUERIES` 五条 PromQL，打 Prometheus | 五条 Metrics API v2 查询，打 Langfuse |
| `_prom_rules()` 读 `/api/v1/rules` | ⚠️ **行不通**——告警没有公开 API，见 §8.3 |
| `PROMETHEUS_URL` 未配 → `available: false` | 换成 `LANGFUSE_*` 未配 → `available: false`（**语义与降级路径完全保留**） |

⚠️ **`or vector(0)` 那条修补要移植，而且坑更大了**。实测空结果**有两种形态**：

| 场景 | 实际返回 |
|---|---|
| 不分组、窗口无数据 | `{"data":[{"count_count":0,"p95_latency":null}]}` ← **latency 是 `null`，不是 0** |
| 有分组、窗口无数据 | `{"data":[]}` ← **空数组**（连行都没有） |

所以移植时要处理**三种情况**：`data` 为空数组、`data` 里字段为 `null`、字段真的是 `0`。
直接写 `float(row["p95_latency"])` 会在第二种形态上**抛异常**（`None` 不能转 float），
失败后若被 swallow 就变成静默的「—」，正是 `stats_service.py:203-205` 记的那个坑的放大版。

⚠️ **`token_usage` 用累计值不用 rate** 的教训同样移植：`stats_service.py:211-213` 写明
「问答是稀疏事件，窗口两端数值常常相同，rate 会算成 0（实测踩过 —— 指标明明有 331，rate 报 0）」。

⚠️ **时间窗有下限**：实测 `2026-09-01` 作 `fromTimestamp` 报
`"fromTimestamp must be before toTimestamp"`（**报错信息有误导性**——真实原因不是顺序反了）。
实现时窗口一律取「相对当前时间」（如近 24h），**不要硬编码绝对日期**。

### 8.2 新增指标（决策 5 后半句：「有价值的就加上」）

换成 Langfuse 后**白捡**的、建议加的：

| 新指标 | 为什么值得 | 数据来源 |
|---|---|---|
| **LLM 成本** | 现在**完全没有**。Langfuse 按模型自动算，是它最直接的增值 | Metrics API 的 cost |
| **token 拆输入/输出** | 现在只有一个合计（`sum(llm_tokens_total)`）。generation 的 usage 天然分开 | `usage_details` 按 `input`/`output` 分组 |
| **按节点的耗时 p95** | 现在只有全局 p50/p95。告警规则本来就想按节点分组（`rag.node`），但自建那套没做成面板 | Metrics API 按观测名分组 |

> 前两项是**净增能力**；第三项是把 §3.2.3.3 仪表盘表里那条「节点耗时分位数（**按节点分组**）」
> **真正做出来**——自建栈里它一直只存在于告警规则里，面板从未实现。

### 8.3 告警：`prometheus-rules.yml` → ⚠️ 需要改设计（实测踩空）

**实测结果**：Langfuse **告警没有公开 REST API**。六个候选端点全部 404：

```
GET /api/public/alerts              → 404
GET /api/public/v2/alerts           → 404
GET /api/public/alert-rules         → 404
GET /api/public/monitors            → 404
GET /api/public/automations         → 404
GET /api/public/v2/automations      → 404
```

> Langfuse Alerts **功能是有的**（官方文档确认，UI 里可配，支持 Slack/Webhook 通知），
> 但**只有 UI，没有 API**。所以「后端读告警状态 → 仪表盘标红」这条路**走不通**。

**这直接推翻本文初稿的设计**（初稿写的是「`_prom_rules()` 改读 Langfuse Alerts 状态」）。

#### 替代方案（推荐 A，需负责人确认）

| | A. 后端自己算阈值（**推荐**） | B. 撤掉告警卡 |
|---|---|---|
| 做法 | 后端用 Metrics API v2 取指标 → 与 `app.yaml` 里的阈值比较 → 返回 `firing: true/false` → 前端标红 | 仪表盘不显示告警；告警只在 Langfuse UI 里看 |
| 保住 §4.4 的「面板标红」交付物 | ✅ 保住 | ❌ 丢掉 |
| 阈值配在哪 | `app.yaml`（配置化，与既有做法一致） | Langfuse UI |
| 缺点 | 与 Langfuse UI 里若也配了告警，**两处阈值不同步** | §3.2.3.5 的「加分项⑦ 面板标红」直接没了 |

**推荐 A** 的理由：你要的「阈值标红标定」是 §3.2.3.5 的加分项交付物，
而 B 等于把它删了。A 的实现成本不高——指标已经在取了，只多一步比较。
Langfuse UI 里的 Alerts **仍可配**，用于 Slack/邮件通知，两者不冲突（只是别指望阈值自动同步）。

三条现有规则在 A 方案下的落法（**形状均已实测**）：

| 现规则 | A 方案下怎么写 | 状态 |
|---|---|---|
| `RAGNodeP95High`（节点 p95 > 10s） | 按 `name` 分组取 `p95_latency` → 比阈值。**实测可用** | ✅ 可落地。**这条现在是坏的**（§1.1），换过去顺带修好 |
| `RAGHttp5xxRate`（5xx > 5%） | ⚠️ **不能按 HTTP 状态码**（见下），改用观测的 `level` 字段。**实测可用** | ✅ 可落地（原以为要做不了） |
| `RAGSpanErrorRateHigh` | 建议**删掉** | ⛔ 建议删。原注释写明「本项目当前不标 span 状态，平时恒为 0」——它从来就没触发过 |

> ⚠️ 走 A 方案后，§13.1 未决项 1（Hobby 档 2 条告警额度）**自动消失**——
> 不再需要 Langfuse 侧的告警额度。

#### ⚠️ 实测：HTTP 状态码**不能**当维度 —— 5xx 率改用 `level`

实测把合法 dimension 全列出来了（API 报错信息里会带完整清单），**共 29 个，没有 HTTP 状态码**：

```
id, evaluatorId, isEvaluatorTest, traceId, environment, parentObservationId,
isRootObservation, type, name, level, version, userId, sessionId, tags, release,
traceName, traceRelease, traceVersion, providedModelName, promptName,
promptVersion, startTimeMonth, toolNames, calledToolNames, costType, usageType,
experimentName, experimentDatasetId, experimentId
```

**`metadata.http.status_code` 这种任意元数据字段不能作为 dimension**（试了 4 种写法全 400）。

**改用 `level`**——它本来就是合法维度，且语义正好对得上：

| 做法 | 说明 |
|---|---|
| 中间件里 `status >= 500` | 把 HTTP span 的 `level` 设为 `"ERROR"`（并写 `status_message`） |
| 5xx 率 | `count(level=ERROR) / count(全部)`，两条查询相除 |

**实测结果**：

```
dim=level          → {"data":[{"level":"DEFAULT","count_count":7},{"level":"ERROR","count_count":1}]}
filter level=ERROR → {"data":[{"count_count":1}]}
```

> 这比原来的 PromQL（`spanmetrics_calls_total{http_status_code=~"5.."}`）**更干净**——
> 不用在 collector 里登记维度，也不受「空结果序列不存在」那个坑影响。

**filter 语法**（实测可用）：`{"type":"string","column":"level","operator":"=","value":"ERROR"}`
（注意：`categoryOptions` 等类型用的是 `key` 而非 `column`，别混）

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
# ⚠️ 两个 key 都不配 = 完全不发外网（跑测试、跑 CLI 时就是这种状态）
LANGFUSE_PUBLIC_KEY="pk-lf-..."
LANGFUSE_SECRET_KEY="sk-lf-..."
LANGFUSE_BASE_URL="https://cloud.langfuse.com"   # ⚠️ 必填，见下
```

⚠️⚠️ **必须显式写 base_url**。**已从安装好的 SDK 源码逐行确认**（不是传闻、不是看文档）：

```python
# langfuse/_client/client.py  Langfuse.__init__
self._base_url = (
    base_url                                  # ① 代码显式传参（最优先）
    or os.environ.get(LANGFUSE_BASE_URL)      # ② 环境变量 LANGFUSE_BASE_URL
    or host                                   # ③ 显式 host 参数
    or os.environ.get(LANGFUSE_HOST, "https://cloud.langfuse.com")   # ④ 默认值
)
```

**两个环境变量都认**：`LANGFUSE_BASE_URL`（优先）与 `LANGFUSE_HOST`。
默认值 `https://cloud.langfuse.com` 只有在**两个都不配**时才生效——
对本项目这是**隐私事故级别的默认值**：配了 key 却漏配地址，数据会静默发去公有云。

最稳的做法是**代码里显式传 `base_url=`**（优先级①，绕开环境变量）。
无论用哪种，都由 §12.2 的 `test_langfuse_host_config.py` 断言钉死。

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

### 12.1 动手前的实测（2026-10-11 已全部跑完，附结果）

**验证环境**：独立 3.13 虚拟环境（`langfuse 4.17.0` + `fastapi 0.143.0` + `otel-sdk 1.45.1`），
**未触碰项目主环境**。脱敏与 trace 两项用一个**本地捕获服务器**冒充 Langfuse 云端，
断言的是**实际收到的字节**，不是 SDK 的自我声明。

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 中间件里 `get_current_trace_id()` | ✅ **通过**。中间件内 / 端点内 / 响应头三处**完全一致**（`7eb48f9c…`），且非全 0 |
| 2 | `trace_context` 挂回原 trace | ✅ **通过**。后台观测的 trace_id 与根**逐字符相同**（`deb9480d…`） |
| 3 | `mask_otel_spans` 在导出前跑 | ✅ **通过**。收到的 3 个 OTLP 请求里，**原始学号 `2023123456` 一次都没出现**，第 2 个请求里出现了 `[REDACTED:student_id]`；钩子被调用 1 次 |
| 4 | 从 SDK 取「当前观测名」 | ❌ **不可行**。`obs.name` **不存在**（公开属性清单里没有 `name`）。名字只藏在私有属性 `obs._otel_span.name` 里 |
| 5 | `capture_input/output=False` 挡不挡得住正文 | ✅ **通过**。节点函数返回的正文标记**未出现在发送字节里** |
| 6 | `@observe` 包 **async** 函数 | ✅ **通过**（本项目节点全是 async） |
| 7 | 忘记 `.end()` 的后果 | ✅ **确认会丢**：未 end 的观测**完全没出现在发送字节里**，且**不报错** |
| 8 | 不配密钥时是否真的不发外网 | ✅ **通过**。收到 0 字节。⚠️ 但返回值有坑，见下 |
| 9 | 不配 `host` 时的默认去向 | ⚠️ **确认危险**：从安装好的 SDK 源码读出 `_base_url = https://cloud.langfuse.com` |

#### 由实测得出的三处设计修正（实现时必须照此写）

**修正 1 —— `_current_node()` 改用 contextvar（不再是「兜底」，是唯一方案）**

`obs.name` 取不到，`obs._otel_span.name` 是私有属性（版本一升就可能没）。
唯一干净的路子是让 `observe_node()` 自己记：

```python
# core/observability.py
_current_node: ContextVar[str] = ContextVar("rag_node", default="unknown")

@contextmanager
def observe_node(name: str, ...):
    token = _current_node.set(name)
    try:
        with langfuse.start_as_current_observation(as_type="span",
                                                    name=f"node.{name}", ...):
            yield
    finally:
        _current_node.reset(token)
```

> ⚠️ 这**不违反** §3.2.3.1「后台任务不能靠 contextvars 隐式继承」那条警告——
> 节点执行是**同步的请求内调用链**，contextvars 在此可靠；
> **跨 asyncio 任务的那两处仍然走 `trace_context`**（§5.4），实测已通过（上表 #2）。

**修正 2 —— `current_trace_id()` 必须兜底 `None`**

实测：**未配置密钥时 `get_current_trace_id()` 返回 `None`**，不是 32 位十六进制串。
若直接写进日志，`trace_id` 字段会变成 `null`，**破坏 §6 承诺的「全 0 兜底」契约**（现有 `logging.py` 与前端错误提示都依赖它是合法字符串）。

```python
def current_trace_id() -> str:
    try:
        return langfuse.get_client().get_current_trace_id() or "0" * 32
    except Exception:          # ⚠️ 保留 logging.py:73 那条「日志不能因埋点故障挂掉」
        return "0" * 32
```

**修正 3 —— `LANGFUSE_HOST` 必须显式配置并断言**

实测从 SDK 源码读出默认 `_base_url = https://cloud.langfuse.com`。
**不配 host 就会把数据发去公有云**——这是隐私事故级别的默认值，不是普通的默认值。
除 §10.2 的「必须写死」外，再加一条测试（§12.2）钉死它。

#### 云端部分（2026-10-11 已对真实项目验完，结果见 §12.4 验收 D）

| 项 | 结论 |
|---|---|
| Metrics API v2 实际返回形状 | ✅ 已拿到，回写进 §8.1（含**空结果的两种形态**） |
| Alerts 的 API | ❌ **没有公开 API** → §8.3 已改设计（推荐后端自算阈值） |
| v1 vs v2 接口 | ⚠️ **v1 对本组织全部不可用（410）**，一切走 v2 |
| 云端保留期 | ⚠️ 仍需在 Langfuse 后台核对档位（§13.1 未决项 4） |

### 12.2 新增测试

测试手段与 §12.1 的探针相同：**起一个本地捕获服务器冒充云端，断言实际收到的字节**——
不用 mock 掉 SDK（mock 掉的正是最容易出问题的接缝）。

| 测试 | 断言 | 钉住哪条实测发现 |
|---|---|---|
| `test_langfuse_masking.py` | 构造含学号/手机号的 prompt，断言**导出前**已打码（沿用 `test_span_redaction.py` 的正控思路：既要断言敏感串不在，也要断言该有的字段在） | §12.1 #3 |
| `test_langfuse_no_content_in_span.py` | 节点观测**不含**输入输出（`capture_input/output=False` 的凭据） | §12.1 #5、#6 |
| `test_langfuse_trace_id.py` | 同一次请求的日志 trace_id == 响应头 trace_id == Langfuse 的 trace_id | §12.1 #1 |
| `test_langfuse_trace_id_disabled.py` | **未配置密钥时 `current_trace_id()` 返回 `"0"*32` 而非 `None`**；日志 JSON 里 `trace_id` 是合法字符串 | §12.1 **修正 2**（否则日志字段变 `null`） |
| `test_langfuse_host_config.py` | host 取自配置；**默认值断言为 `https://cloud.langfuse.com`**（写明这个默认值是危险的），未配置时不发外网 | §12.1 #9（隐私事故级默认值） |
| `test_langfuse_span_lifecycle.py` | 流式 `stream_raw` 即使抛异常，generation 也**必须被 `.end()`**——断言观测出现在捕获内容里 | §12.1 #7（忘 end 会**静默丢**，不报错） |
| `test_langfuse_node_name.py` | 节点内 `_current_node()` 返回正确节点名（contextvar 生效） | §12.1 **修正 1** |

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

#### 验收 C：静默失败边界（由 2026-10-11 可行性实测得出，负责人追加）

**A、B 两条验收的是「功能对不对」；C 验的是「错了会不会不吭声」。**
下面三条**都是实测发现的静默失败点**——它们不会报错、不会让页面变红，只会让数据悄悄不对。

| # | 怎么验 | 判据 | 为什么单列（实测依据） |
|---|---|---|---|
| 1 | **不配 `LANGFUSE_*`** 起服务 → 提一次问 → 看 `logs/app.jsonl` | `trace_id` 字段是 **`"000…0"`（32 位）**，**不是 `null`** | 实测：未配置时 `get_current_trace_id()` 返回 **`None`**。不兜底就会写 `null`，破坏契约且**不报错** |
| 2 | 配好密钥起服务 → 在 Langfuse 官网**你自己的项目**里找数据 | 数据出现在**你的项目**里 | 实测：SDK 默认 `_base_url = https://cloud.langfuse.com`。**配错 host 会发去公有云，本地毫无提示**——对本项目这是隐私事故 |
| 3 | 提一个**走流式**的知识型问题 → 去 Langfuse 看 | `generate` 节点的 generation **存在**，且 **usage 有值** | 实测：`start_observation()` **忘了 `.end()` 就完全不出现在导出里**，且**不报错**。流式正是"开始与结束不连续"的那个场景（§5.2），最容易漏 |

> ⚠️ 三条都是**"不验就不会发现"**的类型：功能测试全绿、页面全正常，但数据是错的或丢了。
> 这也正是本次可行性检查的价值所在——**它们全是真跑出来的，不是推出来的。**

#### 验收 D：云端最小验证（2026-10-11 **已对真实项目跑完**，附结果）

对着真实项目 `Knowledge_rag_system` 跑的，**4 项里 3 项通过、1 项踩空**：

| # | 干什么 | 结果 |
|---|---|---|
| 1 | 发一条最小 trace，确认云上收得到 | ✅ **通过**。`GET /api/public/projects` 200；用 Metrics API 反查拿到 `count=2`、`sum_totalTokens=10`（正是发出的 7+3），**证明数据真的落库了** |
| 2 | 摸清 Metrics API v2 的真实形状 | ✅ **通过**。形状已回写进 §8.1；空结果的**两种形态**也拿到了（`[{count:0, latency:null}]` / `[]`） |
| 3 | 配一条告警规则 | ❌ **踩空**：告警**没有公开 API**（6 个候选端点全 404）。§8.3 已按此改设计 |
| 4 | 核对保留期与档位 | ⚠️ **未完成**：需在 Langfuse 后台看档位设置（§13.1 未决项 4） |

**验收 D 还额外炸出一个会返工的问题**：

```
GET /api/public/traces/{id}  →  410
{"error":"LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION",
 "message":"... is a legacy API that is not available to organizations
            created on or after September 16, 2026.
            Migrate this request to GET /api/public/v2/observations?..."}
```

> ⚠️ **本项目所属组织是 2026-09-16 之后创建的，v1 接口全部不可用**。
> 所有接口调用**必须直接走 v2**，不能照抄任何写 v1 的教程或旧文档。
> 这条如果等到实施时才发现，§8 会整个返工——**这正是"先验收再动手"的价值**。

#### 验收结果（2026-10-11 实际执行）

全部**实跑**（真实 DeepSeek 调用、真实 Langfuse 云项目 `Knowledge_rag_system`），证据在 `logs/acceptance/`（未入库），完整报告 `task-14-report.md`。
**方法**：Langfuse 网页界面不可见，凡「去官网核对」一律用**公开 API v2 反查**（`/api/public/v2/observations|metrics|projects`）代替；仪表盘 UI 项用浏览器实看 + 截图（`B-dashboard.png` / `B8deg-ui.png`）。
**失败项处置**：A4 / A7 / C3 三处验收时判 ❌，随后已修复（commit 见各行）。下表与计数保留**验收当时**的结果，缺陷本身不删；`qa_logs.token_usage` 恒 null 与该 bug 无关（该列全库无写入方，本分支之前即如此）。

**验收 A（8 项）：6 通过 / 2 未通过**

| # | 结果 | 实测要点 |
|---|---|---|
| A1 | ✅ | trace `c85ad731…` 反查到，12 个 observation |
| A2 | ✅ | 单根成树：`POST /api/chat/stream` → `resumed` → `node.*` → `llm.*`（generation 为孙），父子正确 |
| A3 | ✅ | 8 个 `node.*` span 各一次（resolve/route/rewrite/retrieve/rerank/build_context/generate/cite） |
| A4 | ❌→✅ | **验收时为 ❌**：**`llm.generate` 的 `usageDetails={}`**；同 trace 非流式 `llm.rewrite` 有 220/157/377 —— 流式 usage 丢失（根因见 C3）。**已修复**（`c03af7f`）：消费方见到答案收尾 `}` 不再 `break`，继续读完尾部那个带 usage 的 chunk |
| A5 | ✅ | 按 `traceId` 维度聚合：提问窗口内**只有 1 条 trace**，未分裂 |
| A6 | ✅（日志一路以 A8 代证） | 响应头 == `qa_logs.trace_id` == Langfuse 反查，三处同为 `c85ad731…`；问答成功路径**不写 app.jsonl**（设计如此），日志机制改由 A8 上传路径证明（日志行 `trace_id` == 响应头 `44a1b9aa…`） |
| A7 | ❌→✅ | **验收时为 ❌（隐私风险）**：「**我的学号是**2024001234…」→ 已打码 ✅；「**学号**2024001234**的学生**…」→ **原文未打码** ❌。根因：脱敏正则跑在 JSON 转义文本上，`(?<!\d)` 被前字转义末位十六进制字符误判（`是` 的转义码末位是字母 `f` → 能打码；`号` 的转义码末位是数字 `7` → `(?<!\d)` 卡住），样本复现 **16/22 前导字会静默卡掉打码**（`A7-prefix-demo.txt`）。**已修复**（`af7ad58`）：打码前先把 SDK 的容器序列化形态解码、打完再按同款参数编码回去 |
| A8 | ✅ | 上传 trace `44a1b9aa…` 下挂 `resumed`（span_id `d26f88125d44f2de`），与 `app.jsonl` `ingest.done` 行完全一致 |

**验收 B（8 项）：7 通过 + 1 记「有值但恒 0」；降级路径 4/4 通过**

| # | 结果 | 实测要点 |
|---|---|---|
| B1 | ✅ | 文档数 10 / 片段数 148 / 问答量 25 / 拒答率 4.8% |
| B2 | ✅ | p50 40.5 ms、p95 4021.45 ms |
| B3 | ✅ | 错误率 `0.010869565217391304`（原样渲染小数，观感而已） |
| B4 | ✅ | token 4314，输入 2154 / 输出 2160 可拆 |
| B5 | ⚠️ **有值但恒 0**（未配价格表） | 面板 `0`；`cost=0.0`、`costDetails={}`。修复在 Langfuse 后台加 `deepseek-flash` 单价，非代码 |
| B6 | ✅ | 节点耗时 p95 8 行（retrieve 2717 / generate 2110 / rewrite 1775 / rerank 1481 / build_context 507 / route 3 / cite 1 / resolve 1） |
| B7 | ✅ | 「正常 / 共 9 条规则」，9 条全 inactive |
| B8 | ✅ | 趋势 / 拒答分布 / 高频问题 / 降级次数 全渲染 |
| B8-降级 | ✅ 4/4 | 死地址 `http://127.0.0.1:59999` 重启后：① 显示「运行指标暂不可用（langfuse_unavailable）」② 业务区块照常 ③ 接口 **200** + `available:false` ④ 提问照常出答案（`event: done`） |

**验收 C（3 项）：2 通过 / 1 未通过**

| # | 结果 | 实测要点 |
|---|---|---|
| C1 | ✅ | 响应头与日志全部 `00000000000000000000000000000000`（32 位字符串）；`app.jsonl` 无一处 `trace_id: null` |
| C2 | ✅ | `/api/public/projects` 唯一条目即 `Knowledge_rag_system`；恢复后新 trace 落库 |
| C3 | ❌→✅ | **验收时为 ❌**：流式 `generate` 的 generation **存在但 usage 无值**。最小实验定性：全量消费 `stream_raw` → 66 tokens；见 `}` 即 break（`generate.py:402` 的行为）→ **0 tokens**。SDK/云端无责，是应用侧早退没读到尾部 usage chunk。**已修复**（`c03af7f`，与 A4 是同一个 bug） |

**全量测试**：`627 passed, 1 warning in 168.76s`（Task 1 基线 563 passed）——只有 1 条第三方 `StarletteDeprecationWarning`（`fastapi/testclient.py`，与改造无关）。
**前端残留扫描**：`Jaeger|16686|Prometheus` 在 `frontend/web/src` **0 命中**。
**配置恢复**：`.env` 与备份逐字节一致；恢复后 `available=true`、新 trace 落库；三个孤儿容器未动（仍在 Up）。
**遗留（验收当时的下一轮清单）**：① 流式 usage 丢失（`generate.py:402` 早退 → 尾部 usage chunk 未读）—— **已修，`c03af7f`**；② 脱敏正则跑在转义文本上导致约 3/4 中文前导字静默失效（需先解码或改 lookbehind）—— **已修，`af7ad58`**；③ B5 价格表在 Langfuse 后台补配（非代码）—— **仍未做**。

---

## 13. 已知风险与不做的事

### 13.1 未决项（实施前需负责人拍板）

| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| 1 | 告警走 A 方案（后端自算阈值） | 保住 §3.2.3.5 的「面板标红」加分项 | ✅ **已定：A**（负责人 2026-10-11 指定） |
| 2 | ~~云平台 2 条告警额度放不下 3 条规则~~ | — | ✅ **已消失**：告警无 API，改走 A 后不再需要额度 |
| 3 | ~~Metrics API 能否按 HTTP 状态码过滤~~ | — | ✅ **已验**：**不能**（29 个合法维度里没有它）。改用 `level=ERROR`，实测可用（§8.3） |
| 4 | **云平台保留期是否满足「15 天」** | §3.2.3.6 原写 Prometheus 保留 15 天 | ⚠️ **唯一待办**：需在 Langfuse 后台看档位设置 |

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
| 验收 C（静默失败边界） | 负责人要求「检查方案是否可实施，再分析并添加需要的验收标准」→ 由可行性实测得出 | §12.4、§12.1 |
| 验收 D（云端最小验证） | 同上 | §12.4、§12.1 末 |
| 验收 D 已完成（2026-10-11，对真实项目） | 负责人提供账号密钥后执行 | §12.4 |

**可行性实测记录**（2026-10-11）：分两轮，**全部是跑出来的，不是推出来的**。

**第一轮**（独立 3.13 环境装 `langfuse 4.17.0` + 本地捕获服务器冒充云端）：
验证 9 项（§12.1），**8 通过、1 不可行**（`obs.name` 不存在）。
产出 **2 处设计必须修正**（`None` 兜底、contextvar 记节点名）与 **1 个确认的危险默认值**（host）。
回写进 §5.1 / §6 / §10.2 / §12.1。

**第二轮**（对真实 Langfuse 项目 `Knowledge_rag_system`）：验收 D 4 项，**3 通过、1 踩空**：

- ✅ 数据真上云（Metrics API 反查 `count=2`、`sum_totalTokens=10`）
- ✅ Metrics API v2 真实形状拿到 → 回写 §8.1（含**空结果的两种形态**）
- ❌ **告警没有公开 API**（6 个端点全 404）→ **推翻初稿设计**，§8.3 改为后端自算阈值
- ⚠️ **v1 接口对本组织全部 410**（组织建于 2026-09-16 后）→ 一切走 v2

> 两轮里有 **3 处推翻了原设计**（`_current_node()`、告警读法、Metrics API 调用形状）
> 和 **1 处会返工的坑**（v1 不可用）。**这些都是文档上看不出来、只有真跑才会暴露的。**

**事实来源**：Langfuse 官方文档五页实测（自托管 / Alerts / Metrics API v2 / Masking / Python SDK 埋点）+
PyPI 元数据实测（`langfuse 4.17.0`）+ 本仓库代码与文档通读 + 本地三容器的 Prometheus/Jaeger 实查询。
**凡标注「实测」的均非推断**；凡未实测的一律在 §12.1 列为动手前必须先验的项。
