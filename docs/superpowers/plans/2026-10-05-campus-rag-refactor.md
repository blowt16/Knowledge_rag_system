# 校园 RAG 系统重构 — 施工计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan milestone-by-milestone. 步骤用 `- [ ]` 复选框跟踪。
> **本计划的执行节奏已由用户指定：每个里程碑结束停下汇报，等确认后再进下一个。**

**Goal:** 按《校园RAG系统重构方案 v1.3》全量重写校园 RAG 检索问答系统——后端 FastAPI + LangGraph、前端 React 单工程双端、检索期 ACL + 版本过滤、可观测性与评测齐备。

**Architecture:** 单进程 FastAPI（`--workers 1`，因 Chroma 内嵌）内跑 LangGraph 无状态图；三处存储分工：PostgreSQL 存结构化元数据（12 张表）、Chroma 存向量 + 过滤字段、BM25S 存稀疏索引。图执行链固定为 `resolve → route → (chat|clarify|knowledge) → rewrite → retrieve → 版本折叠 → RRF → rerank → build_context → generate → cite`。

**Tech Stack:** FastAPI / asyncpg（裸 SQL，不用 ORM）/ LangGraph / Chroma（内嵌）/ BM25S + jieba / BAAI-bge-reranker-v2-m3（本地 GPU）/ DeepSeek（主模型）/ 阿里云 dashscope（嵌入，1024 维）/ OpenTelemetry + Collector + Prometheus + Jaeger / React 19 + Vite + TS + shadcn/ui + ECharts。

**Spec:** `docs/校园RAG系统重构方案.md`（v1.3，5665 行）——**本计划不复制该文档的论证过程，只给"做什么、改哪个文件、怎么验"。凡标 `【文档 X.Y】` 处，施工时以文档原文为准。**

---

## 0. 开工前的环境实测结论（2026-10-05 实跑，非推断）

| 项 | 实测结果 | 对施工的影响 |
|---|---|---|
| Docker daemon | ✅ 运行中（29.5.2） | PG 可以起 |
| `postgres:17-alpine` 镜像 | ✅ 已在本地（424 MB） | 不必再拉 |
| uv / Python | ✅ 0.11.21 / 3.13.13 | 与文档一致 |
| Node / npm | ✅ v22.20.0 / 11.11.1 | 前端可搭 |
| reranker 模型 | ✅ `models/BAAI/` 2.2 GB 在位 | M1 精排可用 |
| 语料 | ✅ `corpus/guet/` 10 份公文 PDF | 与附录 E.1 画像一致 |
| **嵌入模型维度** | ✅ **1024**（`qwen3.7-text-embedding`，实调 API 返回） | 与 Chroma collection 一致，**不需要重建维度** |
| **DeepSeek 端点** | ✅ **就是官方 API**（`https://api.deepseek.com/v1`，`owned_by: "deepseek"`） | 无需更换，见 §1.0 |
| **`deepseek-flash` + 关思考** | ✅ **定为主模型**：`reasoning_effort:"none"` 后 `ptok=36→10`、`reasoning=0字` | 用户的指定口径 |
| `deepseek-flash` 元数据 | ✅ 官方 `/models` 直读：`DeepSeek-V4.1-Flash`、上下文 **1,048,576**、最大输出 **393,216**、默认 effort `high` | 窗口值**不再是"官方标称待验"，是 API 自报** |
| `deepseek-v4-flash` | ✅ 也能调通（同一模型），但官方 id 是 `deepseek-flash` | 用官方 id |
| `deepseek-chat` | ⚠️ 能调通（= flash 非思考模式），但**官方已于 2026-07-24 停止该别名** | **不用它**——随时可能失效 |
| `deepseek-v4-pro` | ⚠️ 思考模式：`reasoning_content` 占满预算、`content` 可能为空 | 不用：要给流式解析器加 reasoning 分流，首字延迟也高 |
| 缺的依赖 | ❌ `bm25s` / `asyncpg` / `PyJWT` / `pptx`（`passlib` 可选，`bcrypt` 已在） | M0 补装；`pptx` 是坏包，见 B.1.1 |
| 已在的依赖 | ✅ `langgraph` / `opentelemetry-sdk` / `mineru` / `chromadb` / `torch` / `jieba` | 不必装 |
| `backend/app` vs 根 `app` 同名 | ✅ 已实测：`uvicorn app.main:app --app-dir backend` 能正确解析到 `backend/app` | 布局可行，无需改名 |

---

## 1. 与文档的偏差（用户已拍板 + 实测倒逼，共 4 条）

> 这 4 条是**对文档的修改**，施工时按本表执行；其余全部照文档。

| # | 文档原口径 | 本次口径 | 依据 / 代价 |
|---|---|---|---|
| **D-1** | 主模型 **qwen3-max**（窗口 262,144 / 最大输出 65,536） | **官方 API（`api.deepseek.com`）+ `deepseek-flash` + `reasoning_effort: "none"`** | 用户拍板。**不用 `deepseek-chat` 别名**（官方已公告 2026-07-24 退役）。窗口 **1,048,576** / 最大输出 393,216 —— **API 自报值**，非文档推测。**代价**：3.8.2 的 ≈196K 硬上限数字变了，但该路径自述"几乎不会触发"（10 轮会话 ≈15K token），不阻塞。详见 **§1.0** |
| **D-2** | 摘要模型 **qwen-turbo**（因 qwen3-max 输出 24 元/百万 token） | 摘要也用 **`deepseek-flash`（非思考）** | 用户口径「chat 模型都用 deepseek-flash 非思考模式」。主模型本身已便宜，再分一个小模型没有收益。**保留 `summary_model` 配置项**，将来要换只改配置 |
| **D-3** | token 计数用 `dashscope.tokenizers.get_tokenizer("qwen3-max")` | **仍用该分词器，但定位为"近似"** | DeepSeek 分词器不可离线获取（要下 tokenizer 文件）。中文 BPE 量级相近，且水位线本身是成本目标而非硬限。**M3 用真实数据校准**（文档实测得 1.72 字符/token，本次记为上界近似） |
| **D-4** | 视觉模型 `qwen-vl-max` / `qwen3.7-plus` | **本轮不接入** | 两路分支已删 VL 流水线【文档 E.4.5】，无消费方 |

**未偏离、必须照做的关键口径**（易被"顺手简化"掉，列出以防）：`chunk_id = f"{document_id}:{chunk_index}"`；`page_num = batch_start + page_idx`（MinerU 页码重映射）；竖排检测**必须在清洗之前**；`--workers 1`；`/images` 静态挂载**必须删除**；advisory lock **必须与事务同生共死**。

### 1.0 主模型口径：官方 API + `deepseek-flash` 非思考模式（2026-10-05 实测定案）

> **一处更正**：本计划初稿曾把 `api.deepseek.com` 判为"中转站"——**那个判断是错的**。
> `GET /models` 返回 `owned_by: "deepseek"`，且域名就是官方文档给的 `https://api.deepseek.com`。
> 误判原因：我拿训练记忆里的旧模型名（`deepseek-chat` / `deepseek-reasoner`）去套，见 `/models` 只返回两个没见过的名字就下了"这不是官方"的结论。**已按实测改正。**

**官方 `/models` 返回的元数据（原文，非推测）**

| id | `name` | `context_window` | `max_output_tokens` | 默认 effort | 输入模态 |
|---|---|---|---|---|---|
| **`deepseek-flash`** | **DeepSeek-V4.1-Flash** | **1,048,576** | **393,216** | `high` | text, image |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | 1,048,576 | 393,216 | `high` | text |

**定案配置**（`backend/app/config/app.yaml`）

```yaml
llm:
  provider: deepseek
  base_url_env: DEEPSEEK_BASE_URL       # 官方 https://api.deepseek.com/v1
  model: deepseek-flash                 # 官方 id（不是 deepseek-chat 旧别名，也不用加 v4）
  reasoning_effort: "none"              # ★ 非思考模式
  context_window: 1048576               # API 自报值，非文档推测
  max_output_tokens: 8192               # 运行时保守值（API 上限 393216），M5 按需要调
```

**实测证据：关思考确实生效**

| 调用 | `prompt_tokens` | `reasoning_content` | `content` |
|---|---|---|---|
| `deepseek-flash` 默认 | 36 | 196 字 | 41 字 |
| **`deepseek-flash` + `reasoning_effort:"none"`** | **10** | **0 字** | 47 字 |
| `deepseek-chat`（旧别名） | 10 | 0 字 | 28 字 |

两处 `ptok=10 / reason=0` 与旧别名逐项一致 → **`reasoning_effort:"none"` 就是原 `deepseek-chat` 的行为**，正是要的口径。

**三条实测细节（避免踩坑）**

1. **`reasoning_effort: "none"` 不在官方 `supported_levels` 里**（API 自报 `['low','high','max']`），但**实测生效**。
   备选写法 `thinking: {"type": "disabled"}` **同样实测生效**。两者都做成配置项，主用一个、另一个作后备。
2. **`enable_thinking: false` 不生效**——`ptok` 仍 36、`reason` 仍 200 字。**别用它**。
3. **关思考是必须的，不是优化**：思考模式下 `content` 可能为空（小 `max_tokens` 时实测 `content=0字 / reasoning=120字`），
   而 3.5.3 节点 9 的流式解析器**只认 `content` 里的 JSON**。

**`deepseek-flash` 支持图片输入**（`input_modalities` 含 `image`）——但**本轮用不上**，VL 流水线已删除【E.4.5】。记在这里免得将来看到 `image_paths` 时误以为要接。

**为什么不用 `deepseek-v4-pro`**：思考模式占满输出预算、`content` 为空，流式解析器要多分一路 `reasoning_content`，首字延迟也高。**本轮全部 chat 用途（生成、消解、路由、改写、澄清、摘要）统一用 `deepseek-flash` 非思考模式。**

**不使用阿里云 dashscope 的 `deepseek-v4-pro` 兜底**：旧 `factory.py` 有一条 `ALIYUN_BASE_URL + DEEPSEEK_ALIYUN_MODEL` 的 fallback 分支，**新后端不迁**——用户口径是官方 API，且引入第二家会让"这次请求到底走的谁"变得不可知。**评测与排障都需要可复现**。

---

### 1.1 需用户执行的事项（我做不了，列在这里免得漏）

| # | 事项 | 为什么我做不了 | 时机 |
|---|---|---|---|
| U-1 | **吊销并重发 MinerU token** | 附录 A #1：旧密钥硬编码在 `app/config/chroma.yaml` **且已提交进 git 历史**。**轮换密钥要在 MinerU 控制台操作**（我无凭据、也不该有）。新 token 写进 `.env`（已被 gitignore） | M0 开工前 |
| U-2 | **git 历史清理**（同一问题的另一半） | 清历史要 `git filter-repo` + **force push**，与本轮「禁止 force push」直接冲突。**建议：本轮不做，靠 U-1 轮换让泄露的旧密钥作废即可**（轮换后旧密钥失效，历史里那串字符不再有危害）。是否要清历史，等你验收后单独决定 | 验收后决定 |
| U-3 | **MinerU token 有效期** | 附录 E.9 实测：过期时 `POST /file-urls/batch` 返回 `401 {"msgCode":"A0211"}`。**token 会再次过期**——撞到 401 先看 `msgCode`，别怀疑代码 | 遇到时 |
| U-4 | **答辩前启动 Docker Desktop** | 换 PG 后应用**硬依赖 Docker daemon**（附录 G）。SQLite 版没这个问题 | 演示前 |

> ⚠️ **关于 U-1 的现状**：`.env` 里的 `MINERU_TOKEN` 现在是**可用**的（附录 F 实测解析成功过一次）。**在没有轮换之前，远端仍能用这串已进过 git 历史的密钥**——这是本轮唯一带安全债的项，写明在此不隐藏。

---

## 2. 目录布局

```
D:\Knowledge_rag_system\
├── backend/                       ← 本次新建（新后端）
│   ├── app/
│   │   ├── main.py  cli.py  db.py
│   │   ├── core/        config logging telemetry security deps exceptions metrics
│   │   ├── api/         auth users chat conversations documents document_access admin eval
│   │   ├── schemas/     auth chat conversation document citation eval common
│   │   ├── graph/       state builder  +  nodes/{resolve,route,chat,clarify,rewrite,retrieve,rerank,build_context,generate,cite,refuse}.py
│   │   ├── retrieval/   bm25 vector fusion reranker filters
│   │   ├── ingestion/   file_type chunker enrich versioning pipeline  +  loaders/{pdf,docx,pptx,md,txt}.py
│   │   ├── services/    chat_service document_service conversation_service stats_service index_service eval_service
│   │   └── config/      app.yaml security.yaml prompts/
│   ├── migrations/      001_init.sql
│   └── tests/           unit/ integration/ fixtures/
├── frontend/web/                  ← 本次新建（React 双端）
├── docker-compose.yml             ← 本次新建（PG；M5 加三个可观测容器）
├── .env                           ← 本次追加 PG_* / ADMIN_* / JWT_SECRET 等键
├── pyproject.toml                 ← 本次追加后端新依赖（单一 venv，不另建）
└── app/  main.py  front/          ← 旧代码，**冻结不动**（决策 #1：作参考）
```

**为什么用 `backend/` 而不是把新代码覆盖到根 `app/`**：① 文档 3.1 的工程结构就是这个形状；② 旧代码保留可对照（附录 A/B/E 的修复点都要回去查旧实现）；③ 已实测 `--app-dir` 无命名冲突。

**为什么只有一个 venv**：`torch` 4.4 GB + 模型 2.2 GB 重建一次代价太大；文档 C.2.2 的依赖清理也是按"一个 venv"写的。

---

## 3. 里程碑总览

| # | 里程碑 | 一句话交付 | 停下汇报的验收信号 |
|---|---|---|---|
| **M0** | 骨架闭环 | 登录 → 传文档 → 提问 → 流式作答 + 引用，端到端跑通 | 浏览器里真的跑通一次 |
| **M1** | 检索做对 | jieba + BM25S + RRF + 精排降级 + ACL/版本过滤 + 过采样；刷新令牌与登出 | `Recall@5`/`MRR` 有基线；**student 搜不到 `vis_admin` 文档** |
| **M2** | 查询理解 | resolve（含跳过分类 + 注入防护）、三分类路由、clarify（facets）、三类查询扩展 | 多轮指代题通过；澄清误报率有数 |
| **M3** | 生成与引用 | 上下文组装（预算 + 滚动压缩 + 四道防线）、结构化生成、句级引用、原文回跳、后校验 | 点引用跳原文；无依据句能标出 |
| **M4** | React 两端 | User 端补齐（流式渲染/阶段提示/长列表/会话列表）+ 管理端 + 仪表盘 | 全功能可用（评测页除外） |
| **M5** | 评测与打磨 | 校准小集、测试集、ragas、消融实验、可观测性、CI、部署配置 | 消融表产出 + `trace_id` 还原单次请求 |

**每个里程碑结束的动作**：`git tag mN-done` → 汇报（做了什么 / 实测证据 / 偏差 / 下一里程碑计划）→ **等用户确认**。

---

## 4. 全局约定（所有里程碑共享，先读这节）

### 4.1 契约与命名词表（写死在代码里，不得自造）

| 项 | 取值 | 出处 |
|---|---|---|
| 角色 | `student` / `staff` / `admin` | 【3.2.2】 |
| 拒答原因 | `no_candidate` / `insufficient_evidence` | 【5.2】 |
| SSE `error` code | `timeout` / `upstream_error` / `context_length_exceeded` / `internal` / `session_busy`（上传流另加 `batch_not_found`） | 【3.7.2】 |
| `route` | `chat` / `clarify` / `knowledge` | 【3.5.3 节点2】 |
| `resolve_skipped_reason` | `none` / `gate` / `no_history` / `no_feature` / `timeout` | 【3.5.2】 |
| `degradation_events.kind` | `timeout` / `oom` / `model_load_failed` / `index_invalid` / `unavailable` | 【3.3.1】 |
| `ingestion_tasks.status` | `pending`/`parsing`/`chunking`/`embedding`/`done`/`duplicate`/`failed`（**7 个，即进度流 `counts` 的键**） | 【3.7.2】 |
| `documents.status` | `indexing`/`active`/`disabled`/`failed`（**无 `superseded`**） | 【3.3.1】 |
| `stage` | `routing`/`resolving`/`retrieving`/`reranking`/`generating`/`verifying` | 【3.7.2】 |
| 分页上限 | 全站 `page_size ≤ 100`，默认 20 | 【4.3.1.1】 |

### 4.2 三条"不写就会错"的实现铁律

1. **advisory lock 必须与事务同生共死**【3.3.5】
   ```python
   async with conn.transaction():
       await conn.execute("SELECT pg_advisory_xact_lock(hashtext('session:' || $1))", session_id)
       await append_message(conn, ...)          # 写在同一个事务里
   ```
   写成两次独立 `conn.execute` → 锁在真正写之前就释放了，**不报错、只是偶尔并发跑两份**。

2. **版本折叠必须回查 PostgreSQL**，不能在召回集内取最大【3.3.1】。否则现行版措辞与 query 不相似时，会把已废止的旧版当现行返回。

3. **流式 `token` 必须发 JSON 解码后的纯文本**【3.7.2】。残留转义会让前端的置灰偏移**静默错位**，且无法靠任何校验发现。

### 4.3 提交规范

- 全部提交到 **`refactor/campus-rag`**，**禁止动 main、禁止 force push**（用户明令）
- 信息格式：`feat|fix|docs|config|test: 简述`（中文）
- 每个里程碑结束打 tag：`git tag mN-done`
- 提交前过滤密钥：`.env` 已在 `.gitignore`，**新增的 `security.yaml` 只存环境变量名不存值**

---

## 5. M0 — 骨架闭环

**交付**：新工程结构、FastAPI 骨架、LangGraph 图骨架（节点简实现）、PG + Chroma 接通、认证骨架、SSE 协议钉死（含 `Citation`/`VerifyReport` 载荷 schema）、加载层按附录 B + E.4/E.8 迁移修复、最简 React 聊天页。
**验收**：① 端到端跑通：登录 → 传文档 → 提问 → 流式作答 + 引用；② 两条必验有实测记录（**已完成，见 §0 与附录 F，本条销项**）。

### 5.1 改动文件

**新建**
| 文件 | 职责 |
|---|---|
| `docker-compose.yml` | PG 服务（照抄附录 G，含 healthcheck / `127.0.0.1:5432` / named volume） |
| `backend/migrations/001_init.sql` | **12 张表**（照 3.3.1，含 `session_locks`、`UNIQUE(doc_group_id, version)`） |
| `backend/app/db.py` | asyncpg pool（lifespan 创建/关闭）+ `tx()` 助手 + 两层锁（短锁/长锁） |
| `backend/app/core/config.py` | YAML + .env 加载；**禁止硬编码密钥**【3.2.1】 |
| `backend/app/core/logging.py` | JSON 日志 + `trace_id`/`span_id` 注入 + **脱敏**（正文只记 `query_len`/`query_hash`）【3.2.3.2】 |
| `backend/app/core/telemetry.py` | OTel 初始化、`trace_id` 生成与传播、SSE 全流同一 `trace_id`【3.2.3.1】 |
| `backend/app/core/security.py` | JWT 签发/校验（含 `tv` 比对）+ bcrypt 哈希 |
| `backend/app/core/deps.py` | `current_user` / `require_role`；**身份只来自 JWT** |
| `backend/app/core/exceptions.py` | 统一异常 + 全局处理 |
| `backend/app/core/metrics.py` | 占位（M5 落业务指标） |
| `backend/app/api/auth.py` | `login` / `refresh` / `logout` / `me`【3.7.1】 |
| `backend/app/api/chat.py` | `POST /api/chat/stream`（SSE） |
| `backend/app/api/conversations.py` | 会话 CRUD + 消息列表 |
| `backend/app/api/documents.py` | 管理端上传（单文件 + zip）+ 进度 SSE |
| `backend/app/api/document_access.py` | `text` / `file` / `images/{name}`（**走 `filters.py` 同一套 ACL**） |
| `backend/app/schemas/*.py` | Pydantic 契约，**含 `Citation` / `VerifyReport` 完整字段**【3.7.2 节点10】 |
| `backend/app/graph/{state,builder}.py` + `nodes/*.py` | 11 个节点（M0 简实现，M1–M3 逐个填实） |
| `backend/app/retrieval/{filters,vector,bm25,fusion,reranker}.py` | `filters.py` **M0 就要写对**（安全强约束） |
| `backend/app/ingestion/**` | 加载层：`file_type.py` + 5 个 loader + chunker + pipeline |
| `backend/app/services/**` | chat / document / conversation / index |
| `backend/app/cli.py` | `init-db` / `create-admin`（幂等）【附录 D.4】 |
| `frontend/web/**` | Vite + React + TS 骨架、登录页、最简聊天页（fetch + ReadableStream 手工解析 SSE） |

**修改**
| 文件 | 改动 |
|---|---|
| `pyproject.toml` | 追加 `asyncpg`、`bm25s`、`pyjwt`、`langgraph`（已在）、`opentelemetry-*`、`httpx`、`python-jose`→否，用 `pyjwt`；**移出** `streamlit`（附录 A #10） |
| `.env` | 追加 `PG_*`、`ADMIN_USERNAME`/`ADMIN_PASSWORD`、`JWT_SECRET`、`JWT_ALG`、`ACCESS_TTL`、`REFRESH_TTL`；⚠️ **`DEEPSEEK_MODEL=deepseek-chat` 是旧系统的值，新后端不读它**（模型名走 `app.yaml` 的 `deepseek-flash`，见 §1.0） |
| `front/README.md` | 顶部加「⚠️ 本目录已停用，对接的是旧后端接口」 |

### 5.2 任务

**Task M0-0 数据归档与清理（附录 D）——破坏性操作，先归档再清**
- [ ] **停服**（关掉任何持有 Chroma 连接的进程）——**必须在备份之前**，热拷 `chroma.sqlite3` 可能拿到损坏快照【D.3】
- [ ] **归档**：`data/` 与 `db/` 整目录复制到 `data_backup_20261005/`（**不在 .gitignore 覆盖范围内的话要加**）
- [ ] **向用户确认**后才删：`data/chromadb/`、`data/md5_hex_store/`、`data/extracted_images/`、`data/tmp/`、`db/*.db`
- [ ] **绝不动** `models/`（2.2 GB reranker）
- [ ] 验：`ls data/` 只剩空目录；归档目录大小 ≈ 原 `data/` + `db/`
- [ ] **不清理 git 历史**——附录 A #1 要求清历史，但清历史必须 force push，**与用户「禁止 force push」冲突**。改为：**密钥吊销重发由用户执行**（见下方"需用户执行的事项"）

**Task M0-1 依赖与容器**
- [ ] `uv add asyncpg bm25s pyjwt`；`uv add --force-reinstall python-pptx`；`uv run python -c "import pptx; print(pptx.__version__)"` **必须打印出版本号**（B.1.1 说上次只有 `dist-info` 没有 `pptx/` 目录）
- [ ] 走一遍**附录 C.2.2 的依赖清理**：移除 `unstructured` / `markdown` / `openpyxl` / `aiofiles` / `langchain`(总包) / `python-magic` 六个；`modelscope` → `huggingface_hub`（**改代码**：`reorder_service.py:83` 的 `snapshot_download`）
- [ ] ⚠️ 清理后**必须重跑支持格式回归**（C.2.2 的清单：txt/md/pdf/docx/pptx 各传一次）——**任何一项失败立刻回滚该依赖**
- [ ] ⚠️ 「删 modelscope 会让 `rapid_doc`(753MB) 消失」**因果链未经证实**，实测确认，**别当既成收益写进结论**
- [ ] 从 `pyproject.toml` **移除 `streamlit`**，但**不删 `front/`**（附录 A #10）
- [ ] 写 `docker-compose.yml`（照抄附录 G），`docker compose up -d --wait postgres` → 期望 `healthy`
- [ ] 验：`docker exec ... psql -c 'select version()'` 看到 17.11；`netstat` 确认绑在 `127.0.0.1:5432` 而非 `0.0.0.0`
- [ ] 提交 `config: PG 容器、后端新依赖与依赖清理`

**Task M0-2 建表**
- [ ] 写 `migrations/001_init.sql`——12 张表逐字段照 3.3.1；类型口径：PK 用 `TEXT`、`DATETIME`→`TIMESTAMPTZ`、`TEXT(JSON)`→`JSONB`
- [ ] `app/cli.py init-db` 按序号执行 `migrations/*.sql`，**幂等**
- [ ] 测试点 `tests/integration/test_schema.py`：断言 12 张表都存在、`session_locks` 主键是 `session_id`、`documents` 有 `UNIQUE(doc_group_id, version)`
- [ ] 验：`uv run python -m app.cli init-db` 跑两次都不报错
- [ ] 提交 `feat: 12 张表建表脚本与 init-db`

**Task M0-3 配置 + 结构化日志 + trace_id**
- [ ] `core/config.py` 读 `app.yaml` + `.env`；密钥只从环境变量取
- [ ] `core/logging.py` JSON formatter：字段 `ts/level/logger/msg/trace_id/span_id/event` 必填；**用户提问原文、检索片段、答案正文一律不进日志**
- [ ] `core/telemetry.py` 中间件：无 `traceparent` 则新建、有则沿用；响应头回传
- [ ] 测试点 `tests/unit/test_logging_redaction.py`：给一条含学号的 query，断言日志里**搜不到**该学号、但 `query_hash` 存在
- [ ] **LLM 连接自检**（对应 R13）：`cli.py check-llm` 发一条探测请求，断言 **`reasoning_content` 为空**且 `content` 非空——关思考一旦失效要**启动即发现**，而不是等到流式解析器拿不到 JSON
- [ ] 提交 `feat: JSON 结构化日志与 trace_id 贯通`

**Task M0-4 认证骨架**
- [ ] `security.py`：`create_access_token`（15 分钟）、`create_refresh_token`（7 天），载荷带 `tv`
- [ ] `deps.py`：解码 → **查 `users.token_version` 比对** → 不等即 401
- [ ] `api/auth.py` 四个接口；`refresh` 校验签名 + 未过期 + `tv` 匹配，**`refresh_token` 原样返回不轮换**；`logout` 做 `token_version += 1`
- [ ] `cli.py create-admin` 从 `.env` 读 `ADMIN_USERNAME`/`ADMIN_PASSWORD`，建完**提示用户清掉 `.env` 里的口令**
- [ ] 测试点 `tests/integration/test_auth.py`：① 登录拿到两个 token ② 用 access 访问 `/me` 200 ③ `logout` 后**同一 access token 立刻 401**（撤销生效）④ `refresh` 换新 access 200 ⑤ 过期的 access 401
- [ ] 提交 `feat: JWT 认证与 token_version 撤销`

**Task M0-5 filters.py（安全强约束，先于一切检索）**
- [ ] `build_where(user, today, include_restricted=False)` 产出 Chroma `where`：
  ```
  {"$and": [
     {"status": {"$eq": "active"}},
     {"effective_date": {"$lte": today_iso}},
     {"$or": [{"visibility": {"$eq": "public"}}, {"vis_<role>": {"$eq": True}}]}
  ]}
  ```
- [ ] `include_restricted` 只对 `admin` 生效，产出去掉 ACL 那一支的条件；**非 admin 传了按未传处理**（不报错）
- [ ] `can_access(conn, document_id, user, include_restricted)` —— **检索与原文访问共用这一个函数**【3.6】
- [ ] 测试点 `tests/unit/test_filters.py`：① student 的 where 里是 `vis_student` ② `public` 与角色布尔是 `$or` ③ admin 默认**不**旁路 ④ 非 admin 传 `include_restricted=True` 结果与 False 相同
- [ ] 提交 `feat: 检索期 ACL 与版本过滤条件拼装`

**Task M0-6 会话锁（两层）**
- [ ] 短锁：`pg_advisory_xact_lock(hashtext('session:' || $1))`，**与写操作同事务**
- [ ] 长锁：`session_locks` 表，抢锁 SQL 照抄 3.2.4（`ON CONFLICT DO UPDATE ... WHERE expires_at < now()`），释放 SQL **`holder` 必须进 WHERE**
- [ ] TTL 120 秒、心跳 30 秒；`try/finally` 覆盖**正常结束 / 超时 / 客户端断连**三条路径
- [ ] 抢不到 → **409 + SSE `error{code:"session_busy"}`**（拒绝，不排队）
- [ ] 测试点 `tests/integration/test_session_lock.py`：① 同 session 并发第二次 409 ② 锁过期后可抢 ③ **旧 holder 释放删不掉新持有者的锁** ④ 客户端断连后锁被释放
- [ ] 提交 `feat: 两层会话锁与会话并发拒绝`

**Task M0-7 加载层迁移（附录 B + E.4/E.8）**
- [ ] `ingestion/file_type.py`：`sniff_format(head)` / `is_supported(filename, head)`，**三处共用**（上传入口 / zip 内逐文件 / 诊断兜底）【E.4.3】
- [ ] `loaders/pdf.py`：**一次遍历**（类型判定 + 文字提取 + 图片提取）；质量闸门【E.8.1】；竖排检测**在清洗之前**（单字符行 ≥50% → **反转行序**，不是丢弃）；**正则抽章节**（宽松口径，允许行尾带标题）
- [ ] `loaders/pdf.py` OCR 分支：**按页限定范围**调 MinerU（`pages="99-100"`），返回后 **`page_num = batch_start + page_idx`**
- [ ] 空页 → **显式缺失清单**，随上传结果上报（B.1.2）
- [ ] `loaders/{docx,pptx,md,txt}.py`：`page` 语义见 3.4.2（docx/md/txt 恒为 1；**pptx 必须是幻灯片序号**，且 chunk 不跨页）
- [ ] 同步搬 13 条附录 B 修复（B.1.3 已作废，跳过）
- [ ] 测试点 `tests/integration/test_loaders.py`：跑 `corpus/guet/` 全部 10 份 → ① 每份都出 chunk ② **`current_chapter` 非空**（B.2.1/E.3.2 修复的证据）③ `char_start/char_end` 严格递增且 `normalized_text` 切片等于 chunk 正文 ④ 06/07/08/09/10 五份首页**成文日期没被毁掉**（竖排反转早于清洗的证据）
- [ ] 提交 `feat: 加载层迁移（两路分支 + 竖排 + 质量闸门 + 章节正则）`

**Task M0-8 摄入 pipeline + Chroma 写入**
- [ ] 流程严格按 3.4.1：格式校验 → MD5 → 判重（`duplicate`）→ **落盘 + 建行 `status=indexing`（事务内分配 version）** → 解析 → 图片落盘 → 竖排 → 清洗 → **规范化文本写出** → 分块(500/50) → 富化 → 批量向量化 → Chroma → BM25S → **最后翻 `active`**
- [ ] 失败 → **补偿删除**：Chroma chunk → BM25S 项 → 两表置 `failed` **不删行**
- [ ] Chroma metadata 写全 3.3.2 的字段；**`vis_*` 是三个布尔字段，不是数组**
- [ ] version 分配：`max(version)` **包含 `failed`/`indexing` 行**【3.4.3】
- [ ] 测试点：① 同一文件传两次 → 第二次 `duplicate` ② 同 title 传新版 → version=2，旧版仍 `active` ③ **故意让嵌入失败 → 断言 Chroma 无残留、两表行都在且 `failed`** ④ **失败上传后再传新版，version 不撞车**（3.4.3 那条最难排查的错）
- [ ] 提交 `feat: 摄入 pipeline 与补偿删除`

**Task M0-9 SSE 协议钉死（含载荷 schema）**
- [ ] 11 个事件照 3.7.2 实现：`session_created` / `resolved` / `route` / `stage` / `decision` / `token` / `citations` / `verify` / `refused` / `done` / `error`
- [ ] `Citation` 字段全：`marker, document_name, chapter, page, snippet, chunk_id, escalated, images, jump_target{document_id,page,char_start,char_end,boxes[]}`
- [ ] `VerifyReport` 字段全：`total_claims, cited_claims, invalid_markers[], uncited_claims[{sentence,char_start,char_end}]`
- [ ] **`images` 存文件名不是 URL**（签名 URL 5 分钟过期，存 URL 会让刷新后裂图）
- [ ] `error` 是终止事件，**发完不再发 `done`**
- [ ] 提交 `feat: SSE 事件协议与 Citation/VerifyReport 契约`

**Task M0-10 图骨架跑通**
- [ ] `graph/state.py` 定义 `RAGState`（含 ★`evidence` / ★`decision`），`trace` 用 `Annotated[list, operator.add]`
- [ ] `builder.py` 装图，条件边：`route` 三分类、`rerank` 后候选为空 → `refuse`、`generate` 的 `ANSWERED`/`REFUSED`
- [ ] 节点 M0 简实现（能跑通即可，M1–M3 填实）
- [ ] **不使用 checkpointer**；`RAGState` 每轮新建
- [ ] 测试点 `tests/integration/test_graph_smoke.py`：mock 掉 LLM，断言三条路径都能走到 END、`trace` 累积了多个节点（不是只有最后一个）
- [ ] 提交 `feat: LangGraph 骨架与条件边`

**Task M0-11 最简前端 + 端到端**
- [ ] `frontend/web/`：Vite + React + TS；`api/sse.ts` 用 **fetch + ReadableStream 手工解析 `data:` 行**（**不用 `EventSource`**——它发不了 Authorization 头）
- [ ] 登录页 + 最简聊天页（发 `POST /api/chat/stream`、渲染 `token` 流）
- [ ] **M0 验收实跑**：起 PG → `init-db` → `create-admin` → 传 1 份 PDF → 提问 → 看到流式答案 + 引用
- [ ] 提交 `feat: 最简 React 聊天页与端到端闭环`
- [ ] 打 tag `m0-done`，**停下汇报**

---

## 6. M1 — 检索做对

**交付**：jieba 修复 + BM25S 迁移 + 索引持久化、RRF、精排降级、ACL + 版本过滤 + 固定过采样、刷新令牌与登出语义（已在 M0 做，此处回归）。
**验收**：① 检索指标有基线（**不少于 20 道文号/专有名词题，跑 `Recall@5` 与 `MRR`，记数值即算达标**）② ACL 隔离测试通过。

### 6.1 改动文件
`backend/app/retrieval/bm25.py`（新）、`vector.py`、`fusion.py`、`reranker.py`（填实）、`filters.py`（增强）、`backend/app/services/index_service.py`（新）、`backend/tests/fixtures/eval_min20.json`（新）

### 6.2 任务

**Task M1-1 BM25S + jieba + 映射表**
- [ ] `bm25.py`：`jieba.lcut` 分词（**不是 `str.split()`，中文下等于失效**）
- [ ] 索引与**「下标 → `chunk_id`」映射表**同目录落盘、**同生共死**【3.6】
- [ ] 映射表**粒度必须是 `chunk_id` 不是 `document_id`**（否则 RRF 去重失效 + 拿不到 `char_start/page`）
- [ ] 二者任一缺失或版本不匹配 → 视为该路不可用，**不做部分恢复**
- [ ] 测试点：① 存了再读，同一 query 命中相同 `chunk_id` ② 删一个映射表文件 → `is_available()` False ③ 中文 query「缓考」能命中（**这条就是 jieba 修复的证据**）
- [ ] 提交 `feat: BM25S 索引与 chunk_id 映射表`

**Task M1-2 向量检索 + 固定过采样 + 一次重试**
- [ ] 首轮每路 `n_results = 3×K`（K=10，**每路各自取 30，不是合计**）
- [ ] 判据：**「Chroma 过滤 + 版本折叠后」不足 K 条**才重试
- [ ] 重试 `6×K`（60），**整体替换**该路首次结果，**替换后重新折叠**再审
- [ ] 仍不足 → 按实际条数返回
- [ ] 测试点：mock 一个「Top-30 里 28 条被 ACL 滤掉」的场景 → 断言触发了重试且用了 60
- [ ] 提交 `feat: 向量路固定过采样与一次重试`

**Task M1-3 版本折叠（两段式）**
- [ ] 拿候选的 `doc_group_id` **回查 PG** 得 `max(version)`（`active` 且 `effective_date ≤ 今天`）
- [ ] 丢弃 `version ≠ max` 的 chunk；**折叠作用于两路候选的并集**（BM25 侧也要折叠）
- [ ] 折叠产物是**候选池**，后面还有 RRF → 精排 → Top-5
- [ ] 测试点（**这是亮点①的核心测试**）：造 v1/v2 同组文档，**让 v1 的措辞与 query 更相似** → 断言返回的是 v2（回查 PG 生效），且**折叠后不足 K 会触发重试**
- [ ] 提交 `feat: 检索期版本折叠（回查 PG）`

**Task M1-4 RRF 融合**
- [ ] `score(d) = Σ w_i/(k+rank_i(d))`，`k=60`，`w_i` **一律 1.0**
- [ ] 去重键 `chunk_id`；同一 chunk 被多路命中分数累加
- [ ] 融合后**截断 40 条**进精排
- [ ] 测试点：① 多路命中的 chunk 分高于单路 ② 结果里无重复 `chunk_id` ③ 权重全 1.0（消融实验有效性的前提）
- [ ] 提交 `feat: 加权 RRF 融合`

**Task M1-5 精排 + 降级链 + GPU 信号量**
- [ ] `CrossEncoder("models/BAAI/bge-reranker-v2-m3", max_length=512, device="cuda")`，`batch_size=10`
- [ ] **任何异常**（超时/显存/加载失败）→ 同一条路径：跳过精排、按 RRF 顺序返回、记 `degradation_events`，`rerank_degraded=True`
- [ ] `retrieval_confidence`：正常=最高分；降级=**`null`**（**不得填 RRF 分**，量纲不同）
- [ ] `retrieval_confidence` 并入 `qa_logs.node_timings` 的 JSON，**不新增列**
- [ ] GPU **信号量**（同时 2 个）；单路失败继续用另一路；两路都失败 → `candidates=[]` → `refuse`
- [ ] 测试点：① 正常精排顺序正确 ② **把模型路径改成不存在 → 断言降级、`rerank_degraded=True`、答案仍然返回**（这是"精度降一档但不崩"的证据）③ 并发 4 个请求断言信号量生效
- [ ] 提交 `feat: 精排与降级链`

**Task M1-6 检索基线（20 题）**
- [ ] 建 `tests/fixtures/eval_min20.json`：**20 道文号/专有名词题**（从 `corpus/guet/` 出）
- [ ] 脚本跑 `Recall@5` 与 `MRR`，结果写进 `docs/`（**记录数值即达标，不设阈值**——阈值 M5 标定）
- [ ] 提交 `test: 检索基线 20 题与 Recall@5/MRR`
- [ ] 打 tag `m1-done`，**停下汇报**

---

## 7. M2 — 查询理解

**交付**：resolve（条件跳过 + 跳过原因分类 + 注入防护）、三分类路由（规则层 + LLM 兜底 + `last_route` 稳定 + 命中率观测）、clarify 分支（facets + 重新进图）、三类查询扩展、加权 RRF、机制化拒答。
**验收**：多轮指代题通过（题库来自 5.1 第一阶段 60–80 题，**必须在 M2 开工前建好**）。

### 7.1 改动文件
`backend/app/graph/nodes/{resolve,route,chat,clarify,rewrite,refuse}.py`（填实）、`backend/app/services/chat_service.py`、`backend/app/config/prompts/*.txt`（新）、`backend/app/config/app.yaml`（规则词表）、`backend/tests/fixtures/eval_multiturn.json`（新）

### 7.2 任务

**Task M2-1 resolve 节点**
- [ ] 前置闸门 + `route` **共用同一份无信息量词表**，判据是「**整条消息没有实际诉求**」而非「包含一个词」
- [ ] **`knowledge > chat` 优先级同样适用于闸门**——拿不准不跳过
- [ ] 触发条件：有历史 **AND**（含代词 **OR** 含省略特征）；代词用 **jieba 分词后按词匹配**（否则「其他」误命中「他」）
- [ ] 省略特征三条：① 含疑问词且无实义名词 ② 以「呢」结尾 ③ 长度 < N **且不含制度名词**
- [ ] **已删除「长度 < 阈值」触发**；「继续」**不入**无信息量词表
- [ ] 跳过原因**分类记录** `resolve_skipped_reason`（`timeout` 必须与 `gate` 分开——它是失败样本，会污染对照指标）
- [ ] 失败（超时）→ 透传原 query + `timeout`
- [ ] 提示词注入防护三条：声明数据非指令 / 三引号包裹历史 / 输出 JSON 解析 + 长度上限
- [ ] 测试点：①「它需要什么条件？」→ 补全 ②「你好」→ 不调 LLM（`gate`）③「挂科了怎么办」→ **不判成闲聊**、正常进消解与检索 ④ 历史里塞「忽略以上指令」→ 断言检索 query 未被污染 ⑤ LLM 超时 → `timeout` 且透传
- [ ] 提交 `feat: 指代消解与省略补全`

**Task M2-2 route 节点**
- [ ] 规则层**只吃明确的**：`chat` = 仅精确无信息量词表；`knowledge` = 文号正则 + 制度名词 + 疑问词
- [ ] **规则层不判 `clarify`**；**不做「短 → 闲聊」推断**
- [ ] 冲突优先级 `knowledge > chat`
- [ ] LLM 层兜底：解析失败/超时 → **`knowledge`**，且 `route_source` 记 **`llm`**（不是 `rule`）
- [ ] `last_route` **只有 `knowledge` 参与「保持上一轮」**；`chat`/`clarify` 视为无上轮分类
- [ ] 满足 clarify 触发条件时**优先归 `clarify`**（不受上轮约束）
- [ ] 观测 `route_source` 落 `qa_logs`
- [ ] 测试点：①「你好」→ chat ②「你好，我想问下缓考」→ **knowledge**（优先级）③「桂电教〔2025〕28号」→ knowledge ④ 上轮 chat + 本轮「那这个要多久啊」→ **不被粘成 chat** ⑤ LLM 挂了 → knowledge 且 `route_source=llm`
- [ ] 提交 `feat: 三分类路由（规则 + LLM 兜底）`

**Task M2-3 clarify 节点**
- [ ] 触发**仅两种**：`resolve_unresolved=true`、或「`len(resolved_query) < 8` **且** 规则层未命中制度名词/文号」
- [ ] 输出 `{facets:[], question}`；**状态与 SSE 里的字段名是 `clarify_facets`**（写成 `evt.facets` 会让选项永远渲染不出来）
- [ ] 复用节点 9 的流式解析器，**只下发 `question`**
- [ ] 兜底三级：JSON 失败/超时 → 固定问句（仍走 clarify）；`facets` 为空 → 同上；兜底也失败 → `knowledge`
- [ ] 不发 `citations` / `verify`
- [ ] 测试点：① 正常出 facets ② LLM 返回坏 JSON → **仍走 clarify 且有固定问句** ③ 长问题**不**触发澄清（误报是体验最差的失败模式）
- [ ] 提交 `feat: 澄清分支与 facets`

**Task M2-4 rewrite 节点（三类查询）**
- [ ] **1 次** LLM 调用，输出 `[{source,text}]`；**`target`/`weight` 由服务端按固定映射表填**，模型不得输出
- [ ] 映射：`verbatim→both/1.0`、`keywords→bm25/1.0`、`hyde→vector/1.0`
- [ ] 解析失败 → 退化为**一条 `verbatim`**（即标准 hybrid）
- [ ] 提示词强制专名/文号原样保留；不做 paraphrase；含注入防护
- [ ] 测试点：① 文号题 → 文号在 `verbatim` 里一字不改 ② 模型返回非法组合（keywords+vector）→ 被服务端映射表覆盖 ③ 坏 JSON → 退化为 verbatim ④ **权重一律 1.0**
- [ ] 提交 `feat: 三类查询扩展`

**Task M2-5 机制化拒答**
- [ ] `rerank` 后候选为空 → `refuse` 节点：固定话术 + `refused=true, refusal_reason="no_candidate"`
- [ ] `generate` 判不足 → `decision=REFUSED_NO_EVIDENCE` → **服务端固定文案**（忽略模型 answer），`insufficient_evidence`
- [ ] SSE：**两条路都发 `refused`**；`decision` 只在 `generate` 跑过后才发
- [ ] `refused` 载荷带服务端产出的 `text`（**前端不得按 reason 自造文案**）
- [ ] 测试点：① 问库里没有的 → `no_candidate` 且有文案 ② 两条拒答路径的 SSE 事件序列不同（一条有 decision、一条没有）
- [ ] 提交 `feat: 两条拒答路径`

**Task M2-6 多轮指代题验收**
- [ ] 建 `tests/fixtures/eval_multiturn.json`（来自 5.1 的多轮指代型）
- [ ] 跑通并记录；**A 组指标**（澄清误报率 / 澄清命中率 / 路由准确率）需 `eval_cases.expected_route`/`should_clarify` 标注，**建题库时一并标好**
- [ ] 打 tag `m2-done`，**停下汇报**

---

## 8. M3 — 生成与引用

**交付**：上下文组装（**含 3.8 的预算 / 滚动压缩 / 四道防线**）、结构化输出生成、引用统一（句级标记）、页码/章节/偏移定位、原文回跳、声明级后校验。
**验收**：点引用可跳原文；校验能标出无依据句。

### 8.1 改动文件
`backend/app/graph/nodes/{build_context,generate,cite}.py`（填实）、`backend/app/services/context_service.py`（新，3.8 的压缩/预算）、`backend/app/api/document_access.py`（签名 URL）、`frontend/web/src/**`（引用三层入口 + 原文抽屉 + 置灰标注）、`backend/tests/unit/test_{token_budget,stream_json_parser,cite}.py`

### 8.2 任务

**Task M3-1 上下文预算与滚动压缩（3.8）**
- [ ] 配额表：系统提示词 / 历史 16,000 / 检索上下文 **8,000**（防御性上限）/ 当前问题
- [ ] 高水位 `H=12,800`、低水位 `L=8,000`，**按 token 不按条数**
- [ ] 判定式：`count_tokens(compressed_summary) + count_tokens(messages[compressed_count:]) > H` —— **摘要计入预算**
- [ ] 执行：从压缩区最老并入，直到 ≤L；单次至少 6 条；**永不动保留区最近 10 条**
- [ ] **回收顺序 A/B 分开**：A（历史超 16K）**绝不动检索上下文**；只有 B（真溢出硬上限）才动
- [ ] 压缩**在请求路径内同步执行**；失败 → **退回未压缩历史继续作答**，记 `degradation_events(node="compaction")`，**绝不变用户 500**
- [ ] 溢出兜底：捕获 `context_length_exceeded` → 强制压缩一次 → 重试一次
- [ ] `compressed_summary` 与 `compressed_count` **同一事务写**
- [ ] 组装顺序：`[系统][摘要][messages[compressed_count:]][检索上下文][本轮问题]`
- [ ] **组装与计数用的 messages 一律不含本轮**（本轮以 `resolved_query` 单独传）
- [ ] 测试点：① 造一段超 H 的历史 → 压缩到 ≤L ② 摘要超长 → 断言不会无限增长（`summary_max_tokens=800`）③ **摘要模型超时 → 本轮照常返回答案**（硬约束）④ A 触发时**检索上下文条数不变**
- [ ] 提交 `feat: 上下文预算与增量滚动压缩`

**Task M3-2 build_context 节点**
- [ ] 按文档分组 → 组内按 `chunk_index` 排序
- [ ] 裁剪：**整块丢弃**（不块内截断——截断会让 `char_start/bbox` 指向残缺文本）；预算 8,000；「低分」按 **rerank 分**
- [ ] **先裁后编号**（否则编号出现空洞 `[1][3]` 而模型仍照抄）
- [ ] 产出 ★`evidence`——**顺序即 prompt 里的 `[1]…[N]`**
- [ ] 测试点：① 超预算时整块丢弃、无半截 chunk ② 编号连续无空洞 ③ `evidence` 顺序 ≠ `reranked` 顺序（证明不能拿 reranked 做映射）
- [ ] 提交 `feat: 上下文组装与先裁后编号`

**Task M3-3 generate 节点（结构化 + 流式）**
- [ ] 契约 `{"decision":..., "answer":...}`，**无 `citation_numbers`**（编号由服务端从正文派生）
- [ ] 流式解析器四条：① **转义还原**（`\n`/`\"`/`\uXXXX` 发解码后纯文本）② **`answer` 结束判定**：尾部 `"` + 可选空白 + `}`，**回退缓冲 N≥8** ③ **降级目标**：停 token → `error{upstream_error}` → **不发 refused** → 保留已流出 token ④ **`decision` 未到时 token 先缓冲不下发**
- [ ] 首字符校验：去 BOM/空白/```json 围栏后不以 `{` 开头 → 中止并降级
- [ ] 四条硬约束进提示词：句级引用标记 / 适用范围显式 / 未答部分显式声明 / **历史与材料冲突以材料为准**
- [ ] 「结论句」定义**与 cite 共用同一套**
- [ ] 测试点：`test_stream_json_parser.py` ① 答案正文含引号（文号 `〔2025〕28号"`）不被误判结束 ② `\n` 还原成换行 ③ decision 出现在后面时前面的 token 不丢 ④ 前缀是 ```json 时降级
- [ ] 提交 `feat: 结构化生成与流式解析`

**Task M3-4 cite 节点**
- [ ] 一次解析产出 `citations` + `verify_report`
- [ ] 越界判定上界是 **`len(evidence)`**（不是 `len(candidates)`）
- [ ] `invalid_markers` / `uncited_claims` **都带 `char_start/char_end`**（相对**渲染前原始答案**，单位 **Unicode 码点**）
- [ ] `Citation.escalated` 标记越权引用；`images` 存**文件名**
- [ ] `jump_target` 含 `document_id`/`page`/`char_start`/`char_end`/`boxes[]`
- [ ] **不拦截，只标注**（流中可判的越界也只记不拦）
- [ ] 测试点：① 只有 5 条 evidence 却写 `[7]` → `invalid_markers` 有它 ② 无标记的结论句进 `uncited_claims` 且偏移能切出原句 ③ 拿不准的句子**不算**结论句（宁漏不错）
- [ ] 提交 `feat: 引用计算与声明级校验`

**Task M3-5 原文回跳后端**
- [ ] `GET /api/documents/{id}/file`：可见 → 200；不可见且未提权 → **404（不是 403）**；提权 → 200 + `escalated: true` + **审计日志**
- [ ] **必须调 `filters.py` 的同一个函数**（否则 `/file` 就是绕过 ACL 的捷径）
- [ ] `GET /api/documents/{id}/images/{name}` → 返回 **5 分钟签名 URL**；`/images` 静态挂载**已删除**
- [ ] `GET /api/documents/{id}/text` → `{document_id, text, char_offset_index}`
- [ ] 测试点：`test_document_access_acl.py` ① student 拿 admin 文档 id → **404** ② 文档不存在也 404（**两者不可区分**）③ student 带 `include_restricted=true` → 仍 404 ④ admin 提权 → 200 + `escalated` + 审计表有记录 ⑤ 签名 URL 过期后 403
- [ ] 提交 `feat: 原文回跳接口与签名 URL`

**Task M3-6 前端：引用与回跳**
- [ ] 三层入口：行内角标（remark 插件把 `[n]` 文本换成角标）→ 浮层 → 右侧抽屉
- [ ] 四级降级定位：L1 bbox 坐标 → L2 `snippet` 文本匹配 → L3 `chapter` → L4 只跳页 + 提示
- [ ] 引用**按 `jump_target.document_id` 聚合去重**（不是 document_name）
- [ ] 图片缩略图**按 name 现取签名 URL**
- [ ] 置灰标注（4.2.1）：mdast 层做；**叶子按 `value !== undefined` 收集**；**每节点断言「源码切片 === node.value」**；**两道校验 A/B 都要**；失败 → 降级成消息级徽标（**宁可不标，不要标错**）
- [ ] 文案「**未在资料中找到对应依据，建议核对**」（提示而非断言）
- [ ] 测试点：**复用 `data/tmp/d6_probe/` 脚本**回归全部用例与三个坑
- [ ] 提交 `feat: 引用三层入口与原文抽屉`
- [ ] 打 tag `m3-done`，**停下汇报**

---

## 9. M4 — React 两端

**交付**：4.2.4（流式渲染 / 检索过程反馈 / 长列表）+ 会话列表 + 管理端（4.3）+ 仪表盘（4.4）。
**明确不含**：登录、最简聊天、引用展示、消解与澄清交互、置灰标注、原文回跳（M0–M3 已做）；**评测页归 M5**。

### 9.1 改动文件
`frontend/web/src/{views/admin/*, layouts/AdminLayout.tsx, stores/*, router/*, api/schema.d.ts}`、`backend/app/api/{admin,users}.py`、`backend/app/services/stats_service.py`

### 9.2 任务

**Task M4-1 流式渲染与长列表**
- [ ] 按**已完结块**切分，已完结块 `memo` 只渲染一次，每 token 只重渲染尾部未完结块（**不要每个 token 全量重解析**）
- [ ] `stage` 事件驱动阶段提示（文案以 `stage` 为准，`label` 只兜底）
- [ ] 会话列表滚动到底加载下一页；消息流虚拟滚动
- [ ] **用户上翻时停止自动滚动**
- [ ] 断线 → 提示「连接中断，请重新发送」，**不自动重连**
- [ ] 提交 `feat: 流式渲染与长列表`

**Task M4-2 管理端接口**
- [ ] `documents`：列表（筛选 `status`/`visibility`/`q`/分页）、`{group_id}/versions`、`{id}/chunks`（分块预览）、`PATCH`（`title` 不重索引；`visibility`/`visible_roles`/`effective_date`/**`status`** 触发重索引）、`disable`/`enable`、`DELETE`
- [ ] **`status` 也必须重索引**（它冗余在 Chroma，漏了会让 `status="active"` 过滤形同虚设）
- [ ] `users`：列表 / 建号 / 改角色 / 重置口令 —— **三处写操作都要 `token_version += 1`**
- [ ] `roles` 返回 `[{value,label}]`（不在表单里硬编码）
- [ ] `refusals` 明细 + `annotate`（两字段至少填一个，否则 400）
- [ ] `stats/*` 五个接口；**`stats/retrieval` Prometheus 不可用时返回 `available:false` 且 HTTP 200**（不能 500）
- [ ] `trend` **按天补零**；`hot-questions` 默认 Top 10；`refusal_rate` 用 5.2 的自定义定义；`qa_count` **不含评测轮次**
- [ ] 提交 `feat: 管理端接口`

**Task M4-3 管理端页面 + 仪表盘**
- [ ] 文档管理（含上传进度条：显示 `current_file`，不能只画进度条；单文件时 `total=1` 会长时间停在 0%/100%）
- [ ] 版本管理（按 `doc_group_id` 折叠，当前生效高亮）
- [ ] 用户管理、拒答分析（含标注）
- [ ] 仪表盘：业务指标读 PG + 运行指标读 Prometheus，**运行指标区块不可用时业务区块照常渲染**
- [ ] **停用「当前生效版本」时必须显式提示「停用后上一版将恢复生效」**【3.3.1】
- [ ] **改可见范围/生效日必须显式告知耗时并给进度**，不做静默即时保存
- [ ] 提交 `feat: 管理端页面与仪表盘`
- [ ] 打 tag `m4-done`，**停下汇报**

---

## 10. M5 — 评测与打磨

**交付**：10–15 题拒答校准小集、测试集（分两阶段）、ragas 接入、消融实验、测试补齐、可观测性（OTel + trace_id 贯通 + 阈值标红）、CI、部署配置、扫描件与乱码字体 PDF 回归样本各一份。

### 10.1 改动文件
`backend/app/services/eval_service.py`、`backend/app/api/eval.py`、`docker-compose.yml`（加三容器）、`otel-collector-config.yaml`、`prometheus.yml`、`.github/workflows/ci.yml`、`Dockerfile`、`frontend/web/src/views/admin/Eval*.tsx`、`backend/tests/**`

### 10.2 任务

**Task M5-1 题库**
- [ ] `eval_cases` 标注齐：`case_type`/`turns`/`visible_roles`/**`suite`**/`expected_route`/`should_clarify`
- [ ] 先做 **60–80 题**跑通消融与校准；**校准小集 10–15 题标 `suite='refusal_calib'`**
- [ ] **受限文档题 12–15 题**（`case_type='restricted'`，不计入 150–200）——5.3 对照实验要用
- [ ] 提交 `test: 评测题库（分阶段）`

**Task M5-2 评测链路**
- [ ] `POST /api/admin/eval/run` **异步**：立即返回 `run_id`，**复用 `eval_runs.status` 作为任务表**（不新建、不用 SSE）
- [ ] `suite` 参数：省略/`full` → 全量；`refusal_calib` → 只跑小集
- [ ] **评测与线上问答共用同一个 GPU 信号量**（不做优先级抢占）
- [ ] `eval/compare` 服务端产出配置 + 对齐后的指标矩阵，`config_label` **由服务端派生**
- [ ] 提交 `feat: 评测任务链路`

**Task M5-3 消融实验**
- [ ] 主表 8 行逐项叠加：纯向量 → +BM25 → +RRF → +精排 → +verbatim → +keywords → +hyde → 完整链路
- [ ] 多查询几行**额外记录 Rerank 耗时**
- [ ] ACL 对照实验**单独一张表**（不进叠加表）：admin 提权跑全部 / **admin 不提权跑受限题（应为 0）** / student 跑受限题（应为 0）
- [ ] `unauthorized_hits` 直接读 `eval_case_results`
- [ ] 分层表按问题类型拆
- [ ] 提交 `docs: 消融实验表与 ACL 对照实验`

**Task M5-4 可观测性**
- [ ] Collector（含 `spanmetrics` connector）+ Prometheus + Jaeger 三容器，**一并由启动脚本拉起**
- [ ] ⚠️ **必须实测 span 内容捕获已关**：跑一次真实问答，**去 Jaeger 翻 span 属性确认没有正文**（不能只看文档；不同版本开关名不同）
- [ ] `stats/retrieval` 用 PromQL 查；Prometheus 告警规则 + 面板标红
- [ ] **按 `trace_id` 还原单次请求全链路**（验收项）
- [ ] 提交 `feat: OTel 指标栈与 trace 贯通`

**Task M5-5 CI + 部署**
- [ ] GitHub Actions 三个 job：`unit`（含安全用例，每次 push，阻断）/ `contract`（`openapi.json` vs TS 类型 diff，阻断）/ `eval-regression`（手动 + 每周定时，**结果落 `eval_runs`**）
- [ ] 部署四件：`Dockerfile` + 前端构建产物 + 启动脚本（拉三容器）+ `.env` 模板（**不含真实密钥**）+ **`--workers 1`**
- [ ] 提交 `config: CI 与部署配置`

**Task M5-6 回归样本**
- [ ] 造**扫描件 PDF** 与**子集字体/乱码字体 PDF** 各一份（当前语料这两类为 0，不造的话第二路分支与质量闸门是**从没跑过的代码**）
- [ ] 跑通并记录；MinerU 恢复后**多跑几份确认**（只成功过 1 次）
- [ ] 打 tag `m5-done`，**停下汇报** → 进入端到端深度测试

---

## 11. 端到端深度测试方案（全部施工完成后执行）

> 用户指定的 11 个覆盖面全覆盖。**用 `bsk` 驱动真实浏览器**做 UI 部分（按 CLAUDE.md 的真实数据获取约束：`bsk daemon start --port 35000` → `bsk session start` → 用完 `bsk session stop`），后端接口部分用 pytest + httpx 直连。

| # | 覆盖面 | 用例（断言） |
|---|---|---|
| 1 | **认证** | 登录拿 access+refresh；`/me` 200；错误口令 401；无 token 401；过期 access 401；`refresh` 换新 access 200 |
| 2 | **令牌撤销** | 登录 → 记下 access → `logout` → **同一 access 立刻 401**；refresh 也 401；重新登录后可正常访问 |
| 3 | **ACL** | student 提问**检索不到** `visible_roles=["admin"]` 的 chunk；student 拿该文档 id 调 `/file` → **404**；`/images/{name}` → 404；**断言 404 而非 403**（不可探测存在性） |
| 4 | **提权与审计** | admin `include_restricted=true` → 能拿到，且引用带 `escalated: true`；**审计表有该次记录**；admin **不**传该参数 → 拿不到（证明非旁路） |
| 5 | **检索** | 文号题（如「桂电教〔2025〕28号」）能命中正确文档；中文分词生效（「缓考」能召回）；两路都有结果 |
| 6 | **版本过滤** | 传 v2 后，v1 的措辞虽更像 query 但**返回 v2**；停用 v2 → **v1 恢复生效**（且前端有提示） |
| 7 | **Rerank** | 正常：结果按精排分序；**降级：改坏模型路径 → 仍返回答案 + `degradation_events` 有记录 + `rerank_degraded=true`** |
| 8 | **证据判定** | 库里有据的问题 → `ANSWERED`；库里没有的问题 → `REFUSED_NO_EVIDENCE` / `no_candidate`，且 SSE 事件序列正确 |
| 9 | **生成与引用** | 答案含句级 `[n]`；`citations` 的 marker 能映射到 evidence；**故意让 LLM 写 `[99]` → `invalid_markers` 捕获**；无依据句进 `uncited_claims` |
| 10 | **日志** | 按 `trace_id` 能串起一次请求的全部节点日志；**日志里搜不到提问原文/学号**；`query_hash` 存在 |
| 11 | **管理端建号** | admin 建 student 账号 → 该账号能登录 → **`token_version` 使旧令牌失效**（改角色/重置口令后原 token 401） |
| 12 | **会话并发**（补充） | 同 session 并发两次 → 第二次 **409 + `session_busy`**；断连后锁释放 |
| 13 | **上传** | zip 批量 → 进度流 `counts` 七个键都在；**单文件失败不影响整包**；`done` 表示整包结束 |
| 14 | **原文回跳**（UI） | 浏览器点引用 → 抽屉打开 → 定位到页；bbox 缺失的文档能降级到文本匹配 |

**产出**：`docs/端到端测试报告.md` —— 每条用例的**实际输出 / 通过与否 / 失败项 / 修复情况 / 未修复的诚实说明**。

---

## 12. 风险登记册

| # | 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|---|
| R1 | **工程量巨大**，M0–M5 一次做完容易在中途失去方向 | 高 | 高 | 已定：**每个里程碑停下汇报**；每里程碑一个 tag，可回退 |
| R2 | **MinerU 只成功过 1 次**（原症状 5 个任务全卡死） | 中 | 中 | M0 接入时**多跑几份**；失败则扫描件分支降级为「记缺失清单 + 前端可见」，**不阻塞主链路** |
| R3 | **扫描件 / 乱码字体 PDF 样本当前造不出**（语料这两类为 0） | 高 | 中 | M5 专门造样本；**在此之前第二路分支属"从未跑过的代码"**，如实标注 |
| R4 | ~~DeepSeek 上下文窗口未标定~~ **已销项**：官方 `/models` 自报 `context_window=1048576`、`max_output_tokens=393216` | — | — | 值仍做成配置项（换模型不改代码） |
| **R13** | **`reasoning_effort: "none"` 不在官方 `supported_levels`（`['low','high','max']`）里**——虽实测生效，但属未公开取值，将来可能被收紧 | 低 | 中（关不掉思考 → 流式解析器拿不到 `content`） | **双写法都进配置**：主用 `reasoning_effort:"none"`，后备 `thinking:{"type":"disabled"}`（同样实测生效）。**M5 加一条启动自检**：发一条探测请求，断言 `reasoning_content` 为空，否则启动即告警 |
| **R14** | 旧别名 `deepseek-chat` 已于 2026-07-24 退役 | 中 | 高 | **不用它**；配置写官方 id `deepseek-flash` |
| R5 | 前端工作是独立的一大块（React + shadcn + ECharts + PDF 高亮 + docx 预览） | 高 | 中 | 按 M0→M4 渐进，每步可运行；**不做移动端适配**（文档已排除） |
| R6 | Chroma 内嵌 → **只能单 worker** | — | 中 | `--workers 1` 写进部署脚本与文档；**不要因为换了 PG 就以为能多 worker** |
| R7 | 应用启动**硬依赖 Docker daemon** | 中 | 高 | 启动脚本 `docker compose up -d --wait`；答辩前先确认 Docker Desktop 已启动 |
| R8 | `pptx` 上次是坏包（B.1.1） | 中 | 低 | M0-1 显式验证能 `import pptx` |
| R9 | 误把旧索引带进新系统（1244 条《操作系统电子书》残留） | 中 | 高 | 附录 D 全清流程；**必须先停服再备份再删** |
| R10 | 删除 `data/` 是破坏性操作 | — | 高 | **先归档 `data/` 与 `db/` 到 `data_backup_<日期>/`**，再清；归档完向用户确认后才删 |
| R11 | span 属性可能记录问答正文（脱敏漏洞） | 中 | 高 | M5 **实测**去 Jaeger 翻 span，不能只看文档 |
| R12 | 前端不做自动化测试（文档已定） | — | 中 | 4.2.1 标注逻辑靠 `data/tmp/d6_probe/` 脚本手工回归；**写进已知限制** |

---

## 13. 回滚方案

### 13.1 四个层级

| 层级 | 触发情形 | 动作 | 代价 |
|---|---|---|---|
| **L1 提交级** | 某次提交引入 bug | `git revert <sha>` | 分钟级 |
| **L2 任务级** | 某个 Task 做完发现方向错 | `git reset --hard <task 前的 sha>`（**只在本分支**） | 该 Task 的工作量 |
| **L3 里程碑级** | 整个里程碑验收不通过 | `git reset --hard m(N-1)-done` | 该里程碑的工作量 |
| **L4 全量级** | 重构整体失败 | 切回 `main`（**从未被改动**），旧系统 `app/` + `front/` + `main.py` 原样可跑 | 全部重写工作，但**旧系统完好** |

**L4 之所以成立**：本次施工**不修改、不删除**旧 `app/`、`front/`、`main.py`（决策 #1「现有代码作参考」），全部新代码在 `backend/`、`frontend/`。**main 分支全程不动**，是最终的安全网。

### 13.2 数据回滚

| 数据 | 回滚方式 |
|---|---|
| `data/`（chromadb / extracted_images / md5_hex_store） | 附录 D.3 的**停服后归档**——归档目录保留到用户验收通过为止 |
| `db/*.db`（旧 SQLite） | 同上，**不再被新代码读取**，仅作回滚凭据 |
| PostgreSQL | 独立容器 + **named volume** `pgdata`；`docker compose down -v` 即回到空白重来 |
| `models/`（2.2 GB） | **绝不动**——删了要重下 |
| 备份/恢复命令 | `pg_dump -Fc` / `pg_restore`（附录 G）；**备份必须在停服之后** |

### 13.3 硬约束（用户明令 + 不可逆操作）

- ⛔ **不 push 到 main、不 force push**——所有提交只进 `refactor/campus-rag`
- ⛔ **不删 `models/`**、不删旧 `app/`/`front/`
- ⚠️ 删除 `data/`/`db/` 前**必须先归档并告知用户**
- ⚠️ 换嵌入模型（C.2.1）必须**全量重建索引**，不能混用新旧向量

---

## 14. 明确不做（防施工中无限扩张）

| 不做 | 依据 |
|---|---|
| 多租户 / `tenant_id` / RLS | 【3.3.1 口径】 |
| 多环境部署 / K8s / 容量规划 / 值班告警 | 【1.1】 |
| Alertmanager | 【3.2.3.6】 |
| 日志检索平台（ES/Loki）、长期指标存储 | 【3.2.3.6】 |
| `jti` 黑名单、refresh_token 轮换 | 【3.7.1】 |
| checkpointer / `interrupt()` | 【3.5.2 / 节点4】 |
| 组级停用 | 【3.3.1】 |
| chunk 级 diff、chunk 编辑 | 【3.4.3 / 4.3.2】 |
| 原地重跑失败任务 | 【3.3.1】 |
| 语义蕴含判定 / query 分解 | 【节点10】 |
| HTML 格式、xlsx | 【决策 #6】 |
| LibreOffice 转 PDF、pptx 在线预览、移动端适配 | 【4.2.2.6 / 4.2.2.1】 |
| VL 图片描述流水线 | 【E.4.5】 |
| 前端自动化测试 | 【6.2】 |

---

## 15. Review Focus（最可能咬人的输入 / 条件，每条都有归属测试）

> 文档是设计蓝图，它说"必须做什么"，但没穷举"会遇到什么"。以下 8 条是**最可能让真实用户撞上问题**的输入类别——每条的测试已落在对应 Task 里，不是空话。

| # | 输入 / 条件 | 合理预期 | 归属测试 |
|---|---|---|---|
| 1 | **同名文件上传**（学校公文极常见：每年一份「学籍管理规定」） | 应归入同一 `doc_group_id` 成新版本，而不是两个独立文档 | M0-8 ③、M1-3 |
| 2 | **文件名含中文 / 空格 / 超长** | 原文件落盘、`/file` 下载、图片路径全都能正常处理 | M0-8、M3-5 |
| 3 | **提问里含学号 / 姓名**（校园场景必然发生） | 日志里搜不到，只留 `query_hash`；但 `qa_logs` 里能按 trace_id 查到全文 | M0-3 |
| 4 | **LLM 返回非法 JSON / 超时** | 每一层都有稳定降级：resolve 透传、route→knowledge、rewrite→verbatim、clarify→固定问句、generate→error 事件 | M2-1~4、M3-3 |
| 5 | **答案里含引号 / 换行 / emoji**（文号 `〔2025〕28号`、📚） | 流式解析不把正文引号当结束；置灰偏移按**码点**算，星平面字符不漂移 | M3-3、M3-6 |
| 6 | **空 / 纯符号 / 超长 query** | 不崩、不误判成闲聊、不触发无意义澄清 | M2-1、M2-2、M2-3 |
| 7 | **同 session 双击发送 / 多标签页** | 第二次被**拒绝**（409 `session_busy`），不是并发跑两份 | M0-6 |
| 8 | **`effective_date` 是未来日期**（8 月提前传 9 月生效） | 该文档**暂不参与检索**，且旧版继续生效——不能出现政策真空期 | M1-3、E2E#6 |
