# 全链路日志追踪设计方案

- 日期：2026-10-19
- 状态：待评审
- 分支：`refactor/campus-rag`
- 范围：问答图 + LLM + 检索子阶段 + 文档摄入 + 评测后台任务
- 输出：控制台人读文本 + `logs/app.jsonl` 机器可读 JSON

---

## 1. 背景与目标

### 1.1 用户诉求

> 追踪项目运行的所有流程节点，把日志信息打印到控制台中。

目标是让开发者不打开 Langfuse、不查数据库，只盯后端控制台，就能回答：

1. 这次请求经过了哪些节点？
2. 每个节点耗时多少？
3. 哪一步降级了、为什么？
4. 一次文档摄入走到哪一步、卡在哪？
5. 一次评测跑了几题、每题的路线和结果？

### 1.2 成功标准

| 编号 | 标准 | 验证方式 |
|---|---|---|
| S1 | 一次问答在控制台按顺序出现全部图节点，带耗时和关键字段 | 真实发一次提问，肉眼核对 |
| S2 | `LOG_LEVEL=DEBUG` 时能看到向量/BM25/RRF/嵌入/精排批次等子阶段 | 同一次提问切换级别对比 |
| S3 | 一次 PDF 摄入在控制台出现解析、清洗、分块、嵌入、写索引、激活 | 真实上传一份 PDF |
| S4 | 一次评测出现 run 汇总；每题可按需展开 | 跑一轮小评测 |
| S5 | Langfuse 开/关两种状态，同一个 `trace_id` 都能串起一次请求 | 两种环境各跑一次 |
| S6 | `logs/app.jsonl` 仍是合法 JSON，现有字段不破坏 | 解析日志文件 + 跑测试 |
| S7 | 控制台不出现 query / answer / prompt / 检索正文 | grep 日志 + 代码评审 |
| S8 | 不新增第三方依赖 | `pyproject.toml` 无变化 |

### 1.3 非目标

- 不替换 Langfuse，不引入 OpenTelemetry、Jaeger、Prometheus。
- 不引入 loguru / structlog / colorama。
- 不做管理端的日志检索页面。
- 不改数据库 schema（`qa_logs`、`eval_runs` 等保持原样）。
- 不做分布式追踪、多 worker 聚合、日志采样。
- 不把 prompt / 检索正文写进日志。

---

## 2. 现状与问题

### 2.1 已有的东西

| 能力 | 位置 |
|---|---|
| stdlib logging + JSON 格式器 | `backend/app/core/logging.py:63-105` |
| stdout + `logs/app.jsonl` 轮转 | `backend/app/core/logging.py:108-137` |
| `trace_id` / `span_id` 字段 | `JsonFormatter.format` |
| Langfuse HTTP span / 节点 span / LLM generation | `backend/app/main.py:156-222`、`backend/app/graph/builder.py:83-159`、`backend/app/core/llm.py:78-253` |
| 节点耗时 `NodeTrace` | `backend/app/graph/state.py:36-41` |
| 节点耗时落库 | `backend/app/services/chat_service.py:384-406` → `qa_logs.node_timings` |
| SSE stage 事件 | `backend/app/services/chat_service.py:72-110`、`:257-267` |
| 敏感键脱敏 | `backend/app/core/logging.py:25-53` |
| 敏感模式打码（Langfuse 导出前） | `backend/app/core/observability.py:222-319` |

### 2.2 缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| 控制台是原始 JSON | 每条日志一整行 JSON | 人眼扫不出流程 |
| 图节点正常执行不打日志 | 只有降级/失败时 `logger.warning` | 正常请求在控制台几乎静默 |
| Langfuse 关闭时 trace 全 0 | `JsonFormatter` 取 Langfuse，未启用时返回 `"0"*32` | 本地调试无法串联一次请求 |
| 节点耗时只进 Langfuse / DB | 控制台不可见 | 排查慢在哪一步要打开平台 |
| LLM 无控制台日志 | 只有 Langfuse generation | TTFT、tokens 看不到 |
| 检索子阶段无耗时 | 只有少量 warning | 不知道向量/BM25/RRF 各花多久 |
| 摄入只有首尾日志 | `ingest.done` / `ingest.failed` | 解析、分块、嵌入哪步慢不可见 |
| HTTP 无统一首尾日志 | uvicorn 默认格式，与应用 JSON 两套 | 控制台风格混杂 |
| 评测无 trace 关联 | `asyncio.create_task` 后未显式挂 trace | 评测链路难排查 |
| `hash_query` / `query_len` 未使用 | 定义了但没人调用 | 想记 query 特征时无处下手 |

### 2.3 关键约束

- 单 worker 是硬约束（Chroma `PersistentClient` + BM25S 磁盘索引），本方案不改变部署形态。
- 现有 JSON 日志契约被测试锁住：`backend/tests/unit/test_logging_trace_id.py`、`backend/tests/integration/test_chat_trace_log.py`。
- SSE 生成器可能跑在另一个 context，必须显式续接 trace（现有 `observability.attach_trace` 已处理 Langfuse 侧）。
- 日志本身不能因为追踪组件故障而挂掉（`observability.current_trace_id()` 的重入护栏是既有教训）。

---

## 3. 调研结论

调研了 Dify、Haystack、LlamaIndex、LangChain/LangSmith、RAGFlow、Open WebUI、QAnything、LightRAG、Verba、NVIDIA RAG blueprint。与本项目最相关的结论：

| 项目 | 关键做法 | 本方案借鉴 |
|---|---|---|
| **Dify** | `ContextVar` 存 `request_id` / `trace_id`；没 OTel 时用 `uuid5` 生成 32 位兜底；`logging.Filter` 自动注入；text/JSON 双格式；控制台 + 轮转文件 | 本地 trace_id 兜底、双格式器、固定字段 |
| **Haystack** | `Tracer` / `Span` 抽象；`NullTracer` 默认；`LoggingTracer` 在 span 退出时打印 operation + tags；内容字段默认关闭 | span 上下文管理器、内容开关 |
| **LlamaIndex** | `@dispatcher.span` 装饰器；ContextVar 维护父子；handler 可替换（Simple/OTel） | 埋点与输出分离、span 栈 |
| **LangChain** | 控制台 tracer 在阶段结束时打印 `[elapsed]`；`run_id` / `parent_run_id` | 一行一个完成事件、带 elapsed |
| **RAGFlow** | Go 版把 `session_id` 放进 `context.Context`，所有日志自动带上；组件 Enter/Exit/Error + elapsed；日志 level/format/rotation 配置化 | 后台任务按 run 关联、配置化级别 |
| **QAnything** | 手工 `time_record` 贯穿全链路，记录 retrieval/rerank/LLM/token | 字段清单参考（不学其手工传参方式） |

**采纳的 7 条模式**

1. 请求/task 级 ContextVar，日志格式器自动读取，不改业务调用点。
2. 本地 trace_id 兜底，不依赖外部追踪后端。
3. 小号 `Span` 抽象：上下文管理器 + 装饰器，底层可替换。
4. 阶段**完成时打一行**，带 `elapsed_ms` 和稳定字段，不在开始/结束各打一行。
5. 内容默认不记，只记数量/长度/枚举；所有值有长度上限。
6. 应用日志、访问日志、噪音库日志分层；噪音库统一降级。
7. 追踪后端藏在适配层后面，业务代码不直接 import Langfuse/OTel。

**明确不采纳**

- loguru / structlog：新依赖且与现有 stdlib、uvicorn、pytest `caplog` 的兼容成本高于收益。
- OTel Console Exporter：输出吵、不是人读格式；Langfuse 已覆盖 trace UI。
- QAnything 式手工 `time_record` 字典：每条链路都要传参，容易漏。

---

## 4. 总体架构

```text
HTTP 请求
  │
  ├─ trace_middleware
  │    ├─ 生成/续接 trace_id（32 hex）
  │    ├─ trace.begin_trace(trace_id)
  │    ├─ Langfuse span（trace_context 传同一个 trace_id）
  │    ├─ stage=http  完成时 INFO：method / route / status / ms
  │    └─ finally：reset trace、写 x-trace-id / traceparent
  │
  ├─ chat_service.stream_chat
  │    ├─ chat.start（已有）
  │    ├─ chat.first_token（新增）
  │    ├─ 图节点（traced 包装，全部 INFO）
  │    │    ├─ resolve / route / rewrite / retrieve / rerank
  │    │    ├─ build_context / generate / cite / refuse / chat / clarify
  │    │    ├─ 子阶段（DEBUG）：retrieve.vector / retrieve.bm25 / retrieve.rrf
  │    │    └─ LLM（INFO）：llm 完成，带 node / ttft / tokens
  │    └─ chat.done（新增，带 route / decision / 总耗时）
  │
  └─ 两个 sink
       ├─ ConsoleFormatter  → stdout（文本、彩色、缩进）
       └─ JsonFormatter     → logs/app.jsonl（JSON，字段兼容）

后台任务
  ├─ 文档摄入：ingestion_tasks.trace_id → schedule_ingest
  │    └─ trace.begin_trace(trace_id) → ingest.* 阶段
  └─ 评测：eval.run → trace.begin_trace() → eval.run / eval.case → 图节点
```

**核心原则**

- 控制台只是新增一个 sink，不替代 JSON 文件和 Langfuse。
- 所有阶段共用一套 `trace_span`，不各写各的计时。
- 业务代码只关心「阶段名 + 字段」，不关心输出格式和后端。
- 追踪失败不影响主流程。

---

## 5. 核心设计

### 5.1 新模块 `backend/app/core/trace.py`

职责：本地 trace / span 上下文，不 import Langfuse，不 import `observability`（避免循环依赖）。

#### 5.1.1 数据结构

```python
@dataclass
class Span:
    name: str
    span_id: str          # 16 hex
    parent_span_id: str   # 16 hex，根为 ""
    depth: int            # 0 = 根
    started: float        # time.perf_counter()
    fields: dict[str, Any] = field(default_factory=dict)

    def set(self, **fields: Any) -> None:
        """在 with 块内补充最终字段（数量、分数、降级原因等）。"""
        self.fields.update(fields)
```

ContextVar：

```python
_trace_id: ContextVar[str] = ContextVar("rag_trace_id", default="")
_trace_started: ContextVar[float] = ContextVar("rag_trace_started", default=0.0)
_span_stack: ContextVar[tuple[Span, ...]] = ContextVar("rag_span_stack", default=())
```

`_span_stack` 用**不可变 tuple**：`asyncio.create_task` 复制上下文时，父子任务拿到的是同一份快照；push/pop 通过 `set(new_tuple)` 完成，不会互相污染。

#### 5.1.2 公开 API

| API | 说明 |
|---|---|
| `new_trace_id() -> str` | `uuid.uuid4().hex`，32 位 |
| `new_span_id() -> str` | `uuid.uuid4().hex[:16]` |
| `begin_trace(trace_id: str \| None = None)` | 上下文管理器；未传则生成；退出时 reset token |
| `trace_span(name: str, *, layer: str = "stage", level: int = logging.INFO, **fields)` | 上下文管理器，yield `Span`；进入 DEBUG 打 `stage.start`，退出按 `level` 打 `stage.done` / `stage.failed` |
| `stage(name: str, **static_fields)` | 装饰器，兼容 sync/async 函数 |
| `current_trace_id() -> str` | 无 trace 时返回 `""` |
| `current_span_id() -> str` | 栈顶 span_id；无栈返回 `""` |
| `current_stage() -> str` | 栈顶 span.name；无栈返回 `""` |
| `current_depth() -> int` | 栈深度；无栈返回 0 |
| `current_elapsed_ms() -> int` | 从 trace 开始到现在的毫秒数（用于 chat 总耗时） |

#### 5.1.3 `trace_span` 行为

```python
@contextmanager
def trace_span(name, *, layer="stage", level=logging.INFO, **fields):
    parent = _span_stack.get()
    span = Span(
        name=name,
        span_id=new_span_id(),
        parent_span_id=parent[-1].span_id if parent else "",
        depth=len(parent),
        started=time.perf_counter(),
        fields=dict(fields),
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("开始", extra={"event": "stage.start", "stage": name,
                                    "layer": layer, **span.fields})
    token = _span_stack.set(parent + (span,))
    try:
        yield span
    except Exception as e:
        logger.error("失败", exc_info=True,
                     extra={"event": "stage.failed", "stage": name, "layer": layer,
                            "ms": _ms(span.started), "ok": False,
                            "error": type(e).__name__, **span.fields})
        raise
    else:
        logger.log(level, "完成",
                   extra={"event": "stage.done", "stage": name, "layer": layer,
                          "ms": _ms(span.started), "ok": True, **span.fields})
    finally:
        _span_stack.reset(token)
```

约定：

- `stage.done` 是**唯一**的阶段完成日志；调用方不再额外打一条。
- `stage.failed` 一定带 `error=异常类名`，`exc_info=True` 让 JSON 文件保留堆栈；控制台只显示错误类型和消息首行。
- `ms` 由 span 自己算，调用方不要重复传。
- `fields` 里只放数量、长度、枚举、分数；不放 query/answer/正文。
- `layer` 取值：`http` / `node` / `stage` / `llm` / `ingest` / `eval` / `chat` / `context`。
- 追踪自身异常不能影响业务：`new_span_id` / 日志格式化各自兜底；`trace_span` 的 `finally` 必须 reset。

#### 5.1.4 `begin_trace`

```python
@contextmanager
def begin_trace(trace_id: str | None = None):
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    started_token = _trace_started.set(time.perf_counter())
    # ⚠️ 必须同时清空 span 栈：SSE/后台任务会从外层 HTTP 上下文继承 ContextVar，
    #    只换 trace_id 会把外层已关闭的 span 当父节点（实测 depth=1、跨 trace）。
    stack_token = _span_stack.set(())
    try:
        yield tid
    finally:
        _trace_id.reset(token)
        _trace_started.reset(started_token)
        _span_stack.reset(stack_token)
```

- HTTP 中间件：`with trace.begin_trace(tid): ...`
- 后台摄入：`with trace.begin_trace(req.trace_id): ...`
- 评测：`with trace.begin_trace(): ...`
- CLI / 工具：入口处 `with trace.begin_trace(): ...`，控制台也能看到完整链路。

#### 5.1.5 与 Langfuse 对齐

`observability.current_trace_id()` 改为：

```python
def current_trace_id() -> str:
    local = trace.current_trace_id()
    if local:
        return local
    # 原有 Langfuse 取值 + 全 0 兜底保持不变
    ...
```

`observability.current_span_id()`：

```python
def current_span_id() -> str:
    langfuse_id = ...  # 原有逻辑
    if langfuse_id and langfuse_id != _ALL_ZERO_SPAN:
        return langfuse_id
    local = trace.current_span_id()
    return local or _ALL_ZERO_SPAN
```

中间件有 Langfuse 时**始终**传 `trace_context={"trace_id": tid}`（不再只在入站有 `traceparent` 时传），保证：

- 控制台日志的 `trace_id`
- 响应头 `x-trace-id` / `traceparent`
- Langfuse 平台里的 trace

三者是同一个 `trace_id`。

响应头 `traceparent` 的 span 段使用**本地** HTTP span id，与 `http` 日志行的 `span_id` 一致；Langfuse 内部仍用自己的 observation id，靠 `trace_id` 关联。

> ⚠️ 这是本方案唯一会改变既有语义的地方：请求内日志的 `trace_id` 从「Langfuse 关闭时全 0」变为「本地生成的真实 ID」。`test_logging_trace_id.py` 需要同步更新；完全不在任何 trace 上下文里的日志仍保持全 0。

### 5.2 日志格式

#### 5.2.1 控制台 `ConsoleFormatter`

输出结构（固定列）：

```text
HH:MM:SS.mmm LEVEL [trace8] {indent}{stage:<12} {status} {message} {ms} {key=value ...}
```

示例（INFO）：

```text
18:31:02.126 INFO  [a1b2c3d4] resolve     ✔ 完成 332ms   query_len=12 history=3 changed=yes
18:31:02.134 INFO  [a1b2c3d4] route       ✔ 完成 8ms     route=knowledge source=rules
18:31:03.055 INFO  [a1b2c3d4] rewrite     ✔ 完成 921ms   queries=3
18:31:05.772 INFO  [a1b2c3d4] retrieve    ✔ 完成 2717ms  candidates=17 degraded=-
18:31:07.253 INFO  [a1b2c3d4] rerank      ✔ 完成 1481ms  kept=5 top=0.62
18:31:07.760 INFO  [a1b2c3d4] build_context ✔ 完成 507ms   evidence=5 chars=7800
18:31:09.870 INFO  [a1b2c3d4] generate    ✔ 完成 2110ms  decision=ANSWERED chars=520
18:31:09.871 INFO  [a1b2c3d4]     llm       ✔ 完成 2050ms  node=generate ttft=820ms tokens=2345/180
18:31:09.872 INFO  [a1b2c3d4] cite        ✔ 完成 1ms     citations=5
18:31:09.885 INFO  [a1b2c3d4] chat        ✔ 完成 2767ms  route=knowledge decision=ANSWERED
18:31:09.887 INFO  [a1b2c3d4] http        ✔ 完成 200 2769ms
```

DEBUG：

```text
18:31:05.710 DEBUG [a1b2c3d4]   retrieve.vector  ✔ 420ms  hits=30 folded=12
18:31:05.730 DEBUG [a1b2c3d4]   retrieve.bm25    ✔ 18ms   hits=40
18:31:05.750 DEBUG [a1b2c3d4]   retrieve.rrf     ✔ 2ms    in=70 out=17
```

格式规则：

| 规则 | 说明 |
|---|---|
| 时间 | 本地时间 `HH:MM:SS.mmm` |
| 级别 | 5 字符宽；`DEBUG/INFO/WARNING/ERROR` |
| trace | 前 8 位；无 trace 显示 `[--------]` |
| 缩进 | `"  " * depth`；HTTP/chat 为 0，图节点为 1，子阶段为 2 |
| stage | ASCII，左对齐 12 字符；超过 12 不截断，直接顶格 |
| 状态 | `✔` 成功、`✖` 失败、`…` 进行中（DEBUG 开始行） |
| 消息 | 中文：`开始` / `完成` / `失败` |
| 耗时 | `332ms`；≥1000ms 显示 `1.48s`；无 `ms` 不显示 |
| 字段 | 稳定顺序；跳过 `None`/空串；单值最多 120 字符；超过用 `…` |
| 颜色 | `auto`：仅 TTY 且未设置 `NO_COLOR` 时启用；级别/状态/stage 上色 |
| 换行 | 每条日志一行；堆栈只在 JSON 文件里，控制台不打印整段堆栈 |

字段白名单（控制台只显示这些，其余留给 JSON）：

```text
stage layer event ms ok error
query_len query_hash session_id user_id
route route_source decision changed skipped
queries candidates recalled kept top confidence degraded
evidence chars citations reason facets
model node ttft_ms tokens_in tokens_out
hits folded retry
pages ocr_pages tables chunks batches vectors
before_tokens after_tokens summary_chars
case_id turn hit
status method path
```

> 白名单是**显示层**约束，不是安全边界。安全边界仍是「调用方只传计数/枚举/长度」+ `_scrub`。

控制台与 JSON 共用同一套 stage/depth 解析：优先 `record.stage` / `record.layer`（`chat.done`、`http` 这类不在 `trace_span` 里的汇总行），否则取 `trace` 当前栈顶；`depth` 对汇总行取 0。

#### 5.2.2 JSON 文件 `JsonFormatter`

保持现有字段，新增三个可选字段：

```json
{
  "ts": "2026-10-19T18:31:05+0800",
  "level": "INFO",
  "logger": "app.graph.builder",
  "msg": "完成",
  "trace_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "span_id": "0123456789abcdef",
  "event": "stage.done",
  "stage": "retrieve",
  "depth": 1,
  "stage_id": "fedcba9876543210",
  "parent_stage_id": "0123456789abcdef",
  "node": "retrieve",
  "extra": {"ms": 2717, "candidates": 17, "degraded": null}
}
```

- `stage` / `depth` / `stage_id` / `parent_stage_id` 的取值顺序：
  1. 记录上已显式设置的 `record.stage` / `record.layer`（用于 `chat.done`、`http` 这类不在 `trace_span` 里的汇总行）；
  2. 否则从 `trace` 模块读取当前栈顶。
  它们不放进 `extra`；`depth` 对不在 span 里的汇总行取 0。
- `ms` / 业务字段仍在 `extra`，避免改变现有消费方。
- `span_id` 是**本地阶段 span**（`trace_span` 的 span_id），响应头 `traceparent` 的 span 段也用它，保证「响应头 → 那一行日志」能直接对上；`trace_id` 才是与 Langfuse 对齐的 join key。
- 早期方案的「`span_id` 用 Langfuse span」已废弃：Langfuse 的 observation id 只有在其 `with` 内可取，而本地 span 在它外面；两者混用会让 header 与日志的 span_id 不一致（评审实测）。
- 脱敏逻辑不变；`stage` / `depth` / `stage_id` / `parent_stage_id` 加入 `reserved`，避免重复进 `extra`。
- `ConsoleFormatter` 与 `JsonFormatter` 共享同一份 record；控制台格式器不得修改 `record.__dict__`。

#### 5.2.3 级别策略

| 事件 | 级别 | 理由 |
|---|---|---|
| `stage.start` | DEBUG | 默认不刷屏 |
| 图节点 `stage.done` | INFO | 用户要看的主流程 |
| HTTP `stage.done` | INFO | 每次请求一行 |
| chat 汇总 | INFO | 业务结果一行 |
| LLM `stage.done` | INFO | TTFT / tokens 是核心指标 |
| 检索子阶段 | DEBUG | 由节点汇总，默认不展开 |
| 精排批次 | DEBUG | 同上 |
| 摄入阶段 | INFO | 每个文档 8 行左右，可接受 |
| 评测 run 汇总 | INFO | 每轮一行 |
| 评测单题 | DEBUG | 题多时 INFO 会刷屏 |
| 上下文压缩 | INFO | 只在触发时打，频率低 |
| `stage.failed` | ERROR | 失败必须可见 |
| 追踪自身故障 | WARNING | 不回滚业务，但要可见 |

### 5.3 埋点清单

#### 5.3.1 HTTP 入口

位置：`backend/app/main.py:trace_middleware`。

| stage | 级别 | 字段 |
|---|---|---|
| `http` | INFO | `method`、`path`、`status`、`ms` |

- 入口：`trace.begin_trace(tid)`；有 Langfuse 时 `trace_context={"trace_id": tid}`。
- 结束：在 `finally` 里 reset；响应头写同一个 `tid`。
- 路径用路由模板（`request.scope["route"].path`）优先，避免带文档 ID 的高基数路径。
- 不记录 query string、请求体、响应体。
- 5xx 额外 `level=ERROR`；4xx 保持 INFO 但 `ok=False`。

#### 5.3.2 问答图节点

位置：`backend/app/graph/builder.py:traced`。

**关键改造**：现在 Langfuse 未启用时直接 `return await fn(state)`，日志也就没有。改为：

```python
async def _wrapper(state):
    started = time.perf_counter()
    stage = None
    with trace.trace_span(f"node.{node_name}", layer="node") as stage:
        # 原有 Langfuse 逻辑（client 为空则跳过）
        result = await fn(state)
        stage.set(**node_log_fields(node_name, result, state))
    return result
```

- `stage` 名统一为 `node.<name>`；控制台显示 `resolve`、`route` 等（格式器去掉 `node.` 前缀）。
- `layer="node"`。
- 字段来自 `node_log_fields`：

| 节点 | 字段 |
|---|---|
| resolve | `changed`、`skipped`、`query_len` |
| route | `route`、`source`、`clarify_skipped` |
| rewrite | `queries` |
| retrieve | `candidates`、`degraded` |
| rerank | `kept`、`top`、`confidence`、`degraded` |
| build_context | `evidence`、`chars` |
| generate | `decision`、`chars` |
| cite | `citations` |
| refuse | `reason` |
| chat | `decision`、`chars` |
| clarify | `facets` |

- `ms` 由 `trace_span` 计算；`NodeTrace.ms` 仍保留，用于落库，不重复显示。
- 降级信息取 `result["trace"][-1]["degraded"]`，与现有 Langfuse metadata 同源。
- 新增 `node_log_fields` 放在 `builder.py` 或 `graph/node_io.py`，只读 `result`，不读正文。

#### 5.3.3 LLM 调用

位置：`backend/app/core/llm.py:complete`、`stream_raw`。

| stage | 级别 | 字段 |
|---|---|---|
| `llm` | INFO | `node`、`model`、`messages`、`max_tokens`、`ttft_ms`（流式）、`tokens_in`、`tokens_out`、`chars` |

- `complete`：在现有 httpx 调用外包 `trace_span("llm", layer="llm", node=..., model=...)`；成功时 `stage.set(tokens_in=..., tokens_out=..., chars=len(content))`。
- `stream_raw`：在生成器外包 `trace_span`；首个 `piece` 到达时 `stage.set(ttft_ms=...)`；结束时设置 tokens/chars。注意生成器被提前关闭时也要在 `finally` 里结束 span（与现有 Langfuse `finally` 同位置）。
- `node` 取 `observability.current_node()`；上下文压缩调用 LLM 前补 `with observability.node_context("compaction")`，消除 `llm.unknown`。
- 不记录 `messages` 内容、prompt 文本、回答正文。
- 保留 Langfuse generation，不改变其字段。

#### 5.3.4 检索子阶段

| 位置 | stage | 级别 | 字段 |
|---|---|---|---|
| `retrieval/embedding.py:embed_query` | `retrieve.embed_query` | DEBUG | `dim`、`ms` |
| `retrieval/search.py:vector_retrieve` | `retrieve.vector` | DEBUG | `k`、`hits`、`folded`、`retry` |
| `retrieval/search.py:bm25_retrieve` | `retrieve.bm25` | DEBUG | `k`、`hits` |
| `retrieval/fusion.py:weighted_rrf` | `retrieve.rrf` | DEBUG | `in`、`out`、`rrf_k` |
| `retrieval/reranker.py:rerank` | `rerank` | DEBUG | `candidates`、`kept`、`top`、`degraded` |

- 这些 span 嵌套在 `node.retrieve` / `node.rerank` 下，控制台自动缩进。
- 现有 warning（索引失效、降级、重试）保留，级别不变。
- 不在每个 chunk 上打日志；只打批次/汇总。

#### 5.3.5 文档摄入

位置：`backend/app/ingestion/pipeline.py`、`backend/app/services/index_service.py`、`backend/app/services/document_service.py`。

| stage | 级别 | 字段 |
|---|---|---|
| `ingest.schedule` | INFO | `task_id`、`filename`、`size` |
| `ingest.validate` | INFO | `file_type`、`ext` |
| `ingest.dedupe` | INFO | `md5`、`duplicate` |
| `ingest.store` | INFO | `doc_id`、`version` |
| `ingest.parse` | INFO | `pages`、`ocr_pages`、`tables`、`source` |
| `ingest.clean` | INFO | `chars_in`、`chars_out`、`pages` |
| `ingest.chunk` | INFO | `chunks`、`chars` |
| `ingest.enrich` | INFO | `chunks`、`chapters` |
| `ingest.embed` | INFO | `chunks`、`batches`、`dim` |
| `ingest.index_write` | INFO | `vectors`、`bm25` |
| `ingest.activate` | INFO | `doc_id`、`chunks` |
| `ingest.done` | INFO | 以上关键字段汇总 + `ms` |

- `ingest()` 入口 `with trace.begin_trace(req.trace_id):`；`schedule_ingest` 里也显式 `trace.begin_trace(trace_id)`，防止 `asyncio.create_task` 上下文丢失。
- 失败路径在 `except` 里 `logger.error("摄入失败", extra={"event": "ingest.failed", "stage": "ingest", "error": type(e).__name__})`，保留现有 `logger.exception` 的堆栈。
- 解析/OCR 的详细页级日志保持现状（或 DEBUG），不新增逐页 INFO。
- `index_service.build_index` 内部拆 `ingest.embed` 和 `ingest.index_write` 两个 span，避免在 pipeline 里重复计时。

#### 5.3.6 会话与 SSE

位置：`backend/app/services/chat_service.py`、`backend/app/api/chat.py`。

| 事件 | 级别 | 字段 |
|---|---|---|
| `chat.start`（已有） | INFO | `session_id`、完整 `trace_id` |
| `chat.first_token` | INFO | `ttft_ms`、`route` |
| `chat.done` | INFO | `route`、`decision`、`refused`、`latency_ms`、`citations` |
| `chat.disconnected`（已有） | INFO | `session_id`、`elapsed_ms` |
| `chat.error`（异常分支） | ERROR | `error`、`elapsed_ms` |
| `context.compact` | INFO | `before_tokens`、`after_tokens`、`messages`、`summary_chars` |

- `chat.first_token` 在第一次 `yield sse("token", ...)` 时打；用请求开始时间算 TTFT。
- `chat.done` 在发 `done` 事件前打；这是整轮的业务汇总行。
- `chat.start` 的完整 trace_id 保留，方便复制到 Langfuse；节点行只显示前 8 位。
- SSE 生成器入口：
  ```python
  with trace.begin_trace(trace_id), observability.attach_trace(trace_id, span_id):
      ...
  ```
- 上下文压缩在 `context_service` 里包 `trace_span("context.compact", layer="context")`：`before_tokens` 取压缩前的 `history_tokens(summary, messages)`，`after_tokens` 取压缩后的摘要 + 剩余消息；`messages` / `summary_chars` 从 `_compact` 的返回值补。现有 `CompactionTrace`（`ms/recalled/degraded`）继续保留并落库。

#### 5.3.7 评测

位置：`backend/app/eval/runner.py:run_eval`、`backend/app/api/eval.py`。

| stage | 级别 | 字段 |
|---|---|---|
| `eval.run` | INFO | `run_id`、`cases`、`status`、`ms` |
| `eval.case` | DEBUG | `case_id`、`turn`、`route`、`hit`、`ms` |

- `run_eval` 顶部 `with trace.begin_trace():`，生成独立 trace ID；日志里可复制。
- 每题包 `trace_span("eval.case", layer="eval", case_id=...)`；图节点日志自动嵌套。
- 评测是后台任务，不写 `qa_logs`（现有约束不变）。
- 暂不新增 `eval_runs.trace_id` 列；需要时后续迁移。

#### 5.3.8 启动与后台

- `main._warm_reranker` 已有 `event=rerank.preloaded`，控制台可直接读；可补 `stage="rerank.preload"`，不改逻辑。
- `app.startup` / `app.shutdown` 保留。
- `document_access.escalated` 等审计日志保留，不纳入主链路。

### 5.4 配置

`backend/app/config/app.yaml` 的 `logging` 段扩展：

```yaml
logging:
  level_env: LOG_LEVEL
  default_level: INFO
  console_level: DEBUG         # handler 最低级别；实际仍受 root 控制
  console_format: text         # text | json
  console_color: auto          # auto | always | never
  console_max_value_chars: 120
  trace_content: true          # DEBUG 档输出 query / chunk 正文
  trace_content_max_chars: 300
  dir: logs
  max_bytes: 10485760
  backup_count: 5
  redact_body: true
```

规则：

- `LOG_LEVEL` 环境变量仍覆盖 `default_level`；root logger 用它。
- `console_level` 只影响 ConsoleHandler；文件 handler 用 root 级别（保持现有行为）。
- `console_format: json` 时控制台退回 JSON，用于 Docker / 日志采集场景。
- `console_color: never` 用于颜色兼容有问题的终端。
- `LOG_LEVEL=DEBUG` 时子阶段（`retrieve.*`、`rerank`、`eval.case`）自动出现；默认 INFO 只显示主流程。
- `console_level` **不能低于 root 级别**（stdlib logging 在 logger 层就会丢掉 DEBUG 记录）；要看子阶段请设 `LOG_LEVEL=DEBUG`。
- 不新增必需环境变量，现有 `.env` 不需要改。

### 5.5 隐私与脱敏

**硬约束（默认 INFO 档）**

- 不记录：query 原文、resolved_query、answer、prompt、messages、检索正文、context 正文、summary 正文。
- 例外：显式 `LOG_LEVEL=DEBUG` + `logging.trace_content: true` 时，节点详情可输出 query / 改写 / chunk 正文 / answer 前 300 字（`trace_content_max_chars` 可调）；JSON 文件的 `extra` 仍按敏感键打码。生产环境应设 `trace_content: false` 且不长期开 DEBUG。
- 可以记录：`query_len`、`query_hash`（复用 `core/logging.py:hash_query()`）、数量、长度、耗时、分数、模型名、路由/决策枚举、文档名、chunk_id。
- `session_id` / `user_id` 可以记录；它们是内部标识，不是正文。
- 所有字符串值截断到 `console_max_value_chars`，并去掉 CR/LF，防止日志注入。
- 现有 `_scrub` 继续管 `extra` 的敏感键；新增 `stage` / `depth` / `stage_id` / `parent_stage_id` 到 `reserved`。
- `stage.failed` 的 `exc_info=True` 只进 JSON 文件；控制台只打印 `error=类型` 和消息首行。
- Langfuse 的 `mask_otel_spans` 不动；控制台日志不经过它，因此更要靠调用方约束。

### 5.6 错误处理与降级

| 故障 | 行为 |
|---|---|
| `trace_span` 自身异常 | 捕获并 warning，业务继续 |
| Langfuse SDK 异常 | 现有 `try/except` + 重入护栏不变 |
| 日志格式化异常 | `ConsoleFormatter` 用 `str()` 兜底；绝不抛到业务 |
| 控制台非 TTY | 自动禁用颜色，仍输出文本 |
| `trace_id` 非法 | 生成新的；不把客户端字符串直接塞进 SDK |
| 文件 handler 写失败 | stdlib logging 内部处理；不阻塞请求 |
| 阶段中途异常 | `stage.failed` + 堆栈进 JSON + 原异常继续上抛 |

### 5.7 性能

- ContextVar push/pop 为 O(1)，每阶段一次。
- 子阶段日志用 `logger.isEnabledFor(DEBUG)` 保护，默认 INFO 下不构造字段。
- INFO 档每轮问答约 14–18 行；DEBUG 档约 30–50 行。
- 不逐 token 打日志；只记首 token 时间和最终 token 数。
- 摄入只打阶段级，不为每个 chunk/批次打 INFO。
- JSON 文件仍是一行一条，轮转参数不变。

---

## 6. 测试策略

### 6.1 新增单元测试

| 文件 | 用例 |
|---|---|
| `backend/tests/unit/test_trace_context.py` | `begin_trace` 生成 32 hex；嵌套 span 的 parent/depth 正确；退出后 reset；`asyncio.create_task` 能继承 trace；`trace_span` 成功打 `stage.done`、失败打 `stage.failed` 并上抛 |
| `backend/tests/unit/test_console_formatter.py` | 输出包含短 trace、stage、ms、key=value；不输出 JSON；敏感值被截断；非 TTY 无 ANSI；`console_format=json` 时走 JSON |
| `backend/tests/unit/test_node_log_fields.py` | 每个图节点的字段提取正确；不包含正文 |

### 6.2 更新既有测试

| 文件 | 改动 |
|---|---|
| `backend/tests/unit/test_logging_trace_id.py` | 保留「无 trace 上下文时全 0」；新增「`begin_trace` 后为非 0 32 hex」；Langfuse 启用时本地 ID 与 Langfuse ID 一致 |
| `backend/tests/integration/test_chat_trace_log.py` | 响应头 trace_id == 日志 trace_id；新增断言：至少出现 `resolve` / `retrieve` / `generate` 的 `stage.done` |
| `backend/tests/integration/test_stage_events.py` | 不改变 SSE 契约；确认新增日志不影响事件顺序 |

### 6.3 手工验收

1. 启动后端（单 worker），发一次知识型提问：
   - 控制台按顺序出现 resolve → route → rewrite → retrieve → rerank → build_context → generate → cite → chat → HTTP。
   - 每行有 ms 和关键字段。
   - trace_id 非 0，与响应头一致。
2. `LOG_LEVEL=DEBUG` 再发一次：
   - 出现 `retrieve.vector` / `retrieve.bm25` / `retrieve.rrf` / `llm`。
3. 上传一份 PDF：
   - 出现 `ingest.parse` / `ingest.clean` / `ingest.chunk` / `ingest.embed` / `ingest.index_write` / `ingest.activate`。
4. 跑一轮小评测：
   - 出现 `eval.run` 汇总；DEBUG 下出现 `eval.case`。
5. Langfuse 关闭后再跑第 1 步：
   - trace_id 仍非 0，控制台链路完整。
6. 解析 `logs/app.jsonl`：
   - 每行是合法 JSON；`trace_id/span_id/event/stage/depth` 存在；无正文。
7. grep 控制台输出：
   - 不出现提问原文、答案原文、prompt。

---

## 7. 分期实施

### P1：核心 + 控制台 + HTTP + 图节点

- 新增 `core/trace.py`。
- `core/logging.py` 增加 `ConsoleFormatter`，控制台/文件分别挂格式器。
- `main.py` 中间件接入 `begin_trace`，HTTP 完成行。
- `graph/builder.py` 的 `traced` 改为「无 Langfuse 也计时 + 打日志」。
- 更新 `test_logging_trace_id.py`，新增 trace/console 测试。

**验收**：S1、S5、S6、S7、S8。

### P2：LLM + 检索子阶段 + chat 汇总

- `core/llm.py` 接入 `trace_span`，记录 TTFT、tokens。
- `retrieval/*` 子阶段 span。
- `chat_service` 增加 `chat.first_token` / `chat.done` / `chat.error`；压缩 span。
- 新增节点字段测试。

**验收**：S2，且 generate 行能看到 TTFT/tokens。

### P3：摄入 + 评测 + uvicorn 统一

- `ingestion/pipeline.py`、`index_service.py` 阶段 span。
- `eval/runner.py` 接入 trace，run/case 日志。
- `install()` 把 `uvicorn` / `uvicorn.error` / `uvicorn.access` 的 handler 清掉并 `propagate=True`，统一到应用格式器；若实测有重复/丢日志，退回「中间件访问日志 + 关闭 uvicorn access」。
- 噪音库补 `bm25s`。

**验收**：S3、S4。

### P4：收尾

- 全程跑 `pytest backend/tests -q`。
- 更新 `docs/校园RAG系统重构方案.md` 或相关文档中的日志/可观测性章节（只改与本次相关的部分）。
- 确认 `hash_query` / `query_len` 已在节点日志里正式使用（P1/P2 引入）。
- 代码评审：确认没有正文进日志。

---

## 8. 风险与取舍

| 风险 | 影响 | 应对 |
|---|---|---|
| `trace_id` 兜底语义变化 | 既有测试断言全 0 | 更新测试；保留「无 trace 时全 0」 |
| 日志量增加 | 控制台刷屏 | 节点完成 INFO、子阶段 DEBUG、单行 120 字符截断 |
| SSE / 后台任务丢上下文 | 链路断裂 | 显式 `begin_trace` + 现有 `attach_trace`；测试覆盖 |
| 异步生成器提前关闭 | span 不结束 | `finally` 结束 span；测试 `aclose()` |
| uvicorn 日志重复 | 控制台出现双份 | P3 实测；必要时关闭 uvicorn access，仅保留中间件 |
| Windows 颜色兼容 | ANSI 乱码 | `console_color: auto/never`；不加 colorama |
| 日志注入 | 字段含 CR/LF | 所有值去控制字符并截断 |
| 异常消息泄露正文 | 隐私风险 | 阶段日志只传类型；堆栈只进 JSON；代码评审 + grep |
| Langfuse ID 不一致 | 无法按 trace 搜索 | 中间件始终传 `trace_context`；测试断言三方一致 |
| 性能开销 | 高并发下日志变慢 | INFO 约 16 行/请求；DEBUG 才展开；无逐 token 日志 |

**取舍说明**

- 不引入 structlog/loguru：现有代码、测试、uvicorn 都基于 stdlib，换库的收益不抵迁移成本。
- 不接 OTel Console Exporter：控制台需要人读，不是 span dump；Langfuse 已负责 trace UI。
- 不做逐节点开始/结束两行：完成行已包含耗时，开始行只在 DEBUG 出现。
- 不把正文写进日志：排查正文用 `trace_id` 去 `qa_logs` / Langfuse 查。

---

## 9. 后续可扩展

- 在 `trace.py` 后面加 `OpenTelemetryTracer` 适配器，业务代码不变。
- 把 TTFT / token usage 写入 `qa_logs.token_usage`（列已存在，当前未写）。
- 管理端增加「按 trace_id 查日志」页面。
- 按模块配置日志级别（如 `chromadb=WARNING` 已做，后续可配置化）。
- 日志采样 / 慢请求只打详情。

---

## 10. 参考源码

- Dify：
  - [ext_logging.py](https://github.com/langgenius/dify/blob/main/api/extensions/ext_logging.py)
  - [core/logging/context.py](https://github.com/langgenius/dify/blob/main/api/core/logging/context.py)
  - [core/logging/filters.py](https://github.com/langgenius/dify/blob/main/api/core/logging/filters.py)
  - [core/logging/structured_formatter.py](https://github.com/langgenius/dify/blob/main/api/core/logging/structured_formatter.py)
- Haystack：
  - [tracing/tracer.py](https://github.com/deepset-ai/haystack/blob/main/haystack/tracing/tracer.py)
  - [tracing/logging_tracer.py](https://github.com/deepset-ai/haystack/blob/main/haystack/tracing/logging_tracer.py)
  - [logging.py](https://github.com/deepset-ai/haystack/blob/main/haystack/logging.py)
- LlamaIndex：
  - [instrumentation dispatcher](https://github.com/run-llama/llama_index/blob/main/llama-index-instrumentation/src/llama_index_instrumentation/dispatcher.py)
  - [simple span handler](https://github.com/run-llama/llama_index/blob/main/llama-index-instrumentation/src/llama_index_instrumentation/span_handlers/simple.py)
- LangChain：
  - [tracers/stdout.py](https://github.com/langchain-ai/langchain/blob/main/libs/core/langchain_core/tracers/stdout.py)
  - [tracers/base.py](https://github.com/langchain-ai/langchain/blob/main/libs/core/langchain_core/tracers/base.py)
- RAGFlow：
  - [internal/common/logger.go](https://github.com/infiniflow/ragflow/blob/main/internal/common/logger.go)
  - [internal/ingestion/task/debug_log_sink.go](https://github.com/infiniflow/ragflow/blob/main/internal/ingestion/task/debug_log_sink.go)
  - [internal/server/config/log_config.go](https://github.com/infiniflow/ragflow/blob/main/internal/server/config/log_config.go)
- LightRAG：
  - [lightrag/utils.py](https://github.com/HKUDS/LightRAG/blob/main/lightrag/utils.py)
- QAnything：
  - [utils/custom_log.py](https://github.com/netease-youdao/QAnything/blob/main/qanything_kernel/utils/custom_log.py)
  - [core/local_doc_qa.py](https://github.com/netease-youdao/QAnything/blob/main/qanything_kernel/core/local_doc_qa.py)
