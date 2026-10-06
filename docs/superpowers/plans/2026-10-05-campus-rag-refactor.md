# 校园 RAG 系统重构 — 施工计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan milestone-by-milestone. 步骤用 `- [ ]` 复选框跟踪。
> **本计划的执行节奏已由用户指定：每个里程碑结束停下汇报，等确认后再进下一个。**

**Goal:** 按《校园RAG系统重构方案 v1.3》全量重写校园 RAG 检索问答系统——后端 FastAPI + LangGraph、前端 React 单工程双端、检索期 ACL + 版本过滤、可观测性与评测齐备。

**Architecture:** 单进程 FastAPI（`--workers 1`，因 Chroma 内嵌）内跑 LangGraph 无状态图；三处存储分工：PostgreSQL 存结构化元数据（12 张表）、Chroma 存向量 + 过滤字段、BM25S 存稀疏索引。图执行链固定为 `resolve → route → (chat|clarify|knowledge) → rewrite → retrieve → 版本折叠 → RRF → rerank → build_context → generate → cite`。

**Tech Stack:** FastAPI / asyncpg（裸 SQL，不用 ORM）/ LangGraph / Chroma（内嵌）/ BM25S + jieba / BAAI-bge-reranker-v2-m3（本地 GPU）/ DeepSeek（主模型）/ 阿里云 dashscope（嵌入，1024 维）/ OpenTelemetry + Collector + Prometheus + Jaeger / React 19 + Vite + TS + shadcn/ui + ECharts。

**Spec:** `docs/校园RAG系统重构方案.md`（v1.3，5665 行）——**本计划不复制该文档的论证过程，只给"做什么、改哪个文件、怎么验"。凡标 `【文档 X.Y】` 处，施工时以文档原文为准。**

---

## 附录 H：各里程碑开工提示词（新窗口直接粘贴）

> 用法：新开一个窗口，把对应里程碑的整段提示词粘进去。
> 每段都自带"开工读什么、这一阶段的坑在哪、完工要交什么"，不依赖上文记忆。

### H.1 M1 开工提示词（已执行，tag `m1-done`，交接见 §6.9）

```
读 docs/superpowers/plans/2026-10-05-campus-rag-refactor.md，
重点读 §0（施工规程）与 §5.9（M0 完工交接）。

我是这个项目的负责人，你继续做 M1（检索做对）。几条前提：

1. M1 的**代码已经存在**（做 M0 端到端时顺带建出来了），见 §5.9 的对照表。
   **不要重建** —— 照 §6 原版 M1 从零写一遍是纯浪费。

2. M1 实际剩下三件事：
   ① 【先做】把 corpus/guet/ 的 10 份 PDF 全部重新上传入库。
      现在库里只有 1 份 —— M0 修 Chroma $lte 那个 bug 时清空过索引，
      只回填了 1 份验证。**不先补数据，基线数字就是错的。**
   ② 补验收物：20 题文号/专有名词题库（tests/fixtures/eval_min20.json）
      + Recall@5 / MRR 评测脚本；ACL 隔离集成测试（student 查不到 vis_admin 文档）。
   ③ 补一个 M0 实测发现的缺口：reranker 启动预热。
      实测冷启动 23–80 秒（首次 79.5s / 页缓存热 23s），加载后推理只要 0.4–3s。
      现在第一个知识型提问要等一分多钟。预热做进 lifespan，别做成懒加载。

3. 施工注意事项见 §0.3（A1–A12），其中最容易踩的是：
   - A1 改动必须实测验证，不能只推理（M0 有 15 个 bug 是实测抓的）
   - A2 文档的字面读法可能是错的，实测冲突时以实测为准
   - A9 改 filters.py / state.py / 提示词要跑全量测试

4. 环境命令见 §0.4。开工前先确认 PG 起着、66 项测试全过。

5. 规矩：提交只进 refactor/campus-rag，禁止动 main、禁止 force push。
   M1 做完停下汇报（交付 / 实测证据 / 偏差 / 已知问题），
   并把「完工交接」小节写进本文档、打 tag m1-done。

先给我一个 M1 的执行计划（三件事的先后与各自怎么验），我确认后再动手。
```

### H.2 M2 开工提示词（已执行，tag `m2-done`，交接见 §7.9）

```
读 docs/superpowers/plans/2026-10-05-campus-rag-refactor.md，
重点读 §0（施工规程）、§6.9（M1 完工交接）与 §7（M2 原文 —— 那才是你要做的）。

我是这个项目的负责人，你接着做 M2（查询理解）。几条前提：

1. M2 的代码不是从零写 —— 图里 11 个节点在 M0 就建好了**简实现**，
   你要把 resolve / route / clarify / rewrite 这几个填实（§7.1 列了文件）。
   动手前先读 §6.9：M1 的教训是「计划说三件事，实测发现必须先修三个缺陷」，
   别急着照 §7 的任务清单往下做。

2. **开工第一件事：建 tests/fixtures/eval_multiturn.json**（§7 的验收明确要求
   「题库必须在 M2 开工前建好」）。多轮指代题，来自方案 5.1 第一阶段。
   没有它，M2 做到验收时无题可跑。

3. 施工注意事项见 §0.3（A1–A12）。M1 又添了两条**实测**教训，已写进 §6.9：
   - **别让中间层（Chroma / PG / 任何 get 类接口）的返回顺序决定最终排序** ——
     Chroma 的 get 不保证按传入 ids 顺序返回，M1 因此把 BM25 的整个排序丢掉了（B-2）
   - **新增节点必须写 trace**，漏写会让 SSE 事件静默缺失（A3）

4. 环境：PG 起着；`uv run pytest backend/tests -q` 应为 **93 passed**；
   后端单 worker 起在 8090。
   ⚠️ **跑测试或跑 CLI 之前先停掉后端** —— Chroma 内嵌，同一份索引不能两个进程同时写（A10）。

5. 规矩：提交只进 refactor/campus-rag，禁止动 main、禁止 force push。
   M2 做完停下汇报（交付 / 实测证据 / 偏差 / 已知问题），
   并把「完工交接」小节写进本文档、打 tag m2-done。

先给我一个 M2 的执行计划（任务顺序与各自怎么验），我确认后再动手。
```

### H.3 M3 开工提示词（已执行，tag `m3-done`，交接见 §8.9）

```
读 docs/superpowers/plans/2026-10-05-campus-rag-refactor.md，
重点读 §0（施工规程）、§7.9（M2 完工交接）与 §8（M3 原文 —— 那才是你要做的）。

我是这个项目的负责人，你接着做 M3（生成与引用）。几条前提：

1. **M3 与 M1/M2 不同形：这次是一半代码有、一半完全没有。**
   下面是我开工前实测摸过的（不是照 §8.1 推断的），直接按它排计划：

   | §8.2 任务 | 代码实际状态 |
   |---|---|
   | M3-1 上下文预算 + 滚动压缩 | ❌ **完全没有**。`conversations` 表已有 `compressed_summary` / `compressed_count` 列，但没有任何压缩逻辑；`conversation_service.load_history` 里留着一句注释「M3 换成 count_tokens(摘要) + …」 |
   | M3-2 build_context | ✅ 已有（`build_evidence`：整块丢弃 / 分组排序 / **先裁后编号** / `evidence`）—— 要补测试 |
   | M3-3 generate | ✅ 已有（`StreamJsonParser` 四条细节、`precheck_prefix`、`_stream_generate`、`extract_markers`、`is_conclusion_sentence`）—— 要补测试 + 核对提示词四条硬约束 |
   | M3-4 cite | ✅ 已有（`build_citations` / `build_verify_report` / `cite_node`）—— 要补测试 |
   | M3-5 原文回跳后端 | ❌ **完全没有**：没有 `/file`、`/images/{name}`、`/text`，也没有签名 URL |
   | M3-6 前端引用三层 | ❌ **完全没有**：前端只有 9 个文件（登录页 + 最简聊天页），**连 markdown 渲染依赖都没装**（remark / react-markdown 都要新增） |

   所以 M3 的量比 M2 大：两条新链路（压缩 / 文件访问）+ 一整块前端 + 三个节点的护栏测试。

2. **开工第一件事：`uv run pytest backend/tests -q` 应是 200 passed**；PG 起着。

3. ⚠️ **§8.2 M3-6 原来写「复用 data/tmp/d6_probe/ 脚本回归」—— 那个脚本不在本仓库，
   也从未进过 git 历史**（旧项目产物，方案 §3086 提到的）。别去找，找不着。
   **负责人已定（2026-10-05），前端这层分两半验：**
   - **标注逻辑（4.2.1）→ 重新写一个回归脚本**（§8.2 测试点 A）：覆盖全部用例与三个漂移坑；
     **必须 import 前端真实实现**，不许把标注逻辑抄一份进脚本（抄了就脚本绿、页面错）；
     落在仓库内、可重复跑。按文档 6.2 口径，它是**手工回归脚本，不是自动化测试**。
   - **端到端 → 用 bsk 驱动真实浏览器**（§8.2 测试点 B）：点引用真的跳到原文、
     无依据句真的被标出。三个操作坑照 §0.4 与 CLAUDE.md：端口必须显式带
     `--port 35000`（不带的话 `browsers connected` 恒为 0）、会话用完必须
     `bsk session stop`（悬挂会话会堵命令队列）、登录/验证码用 `bsk request-help` 交给用户。

4. 施工注意事项见 §0.3（A1–A12）。M1/M2 又添了这些**实测**教训（§6.9 / §7.9）：
   - **别让中间层（Chroma / PG / 任何 get 类接口）的返回顺序决定最终排序**（M1 的 B-2）
   - **新增节点必须写 trace**，漏写会让 SSE 事件静默缺失（A3）
   - **兜底必须留痕**：节点静默兜底却不写 `degraded`，评测护栏就拦不住（M2 评审 I2）
   - **做对照实验时，两腿只能差一个变量**（M2 评审 I1：消解对照最初把「消解」和
     「查询扩展」一起变了，差就归因不到消解头上）
   - **提示词改动**：`tests/unit/test_prompts.py` 锁着每个提示词的占位符集合 ——
     改 `generate.txt` 或新增提示词要同步登记 `EXPECTED_PLACEHOLDERS`；
     提示词文件开头的 `#` 注释会被剥掉、不进模型
   - **拒答路径的 SSE 事件序列有锁**（`tests/integration/test_refusal_paths.py`）：
     M3 会动 `generate`/`cite`，动了事件顺序会立刻红
   - **批处理入口（CLI）不要引入 `to_thread(torch)`**：Windows 上「asyncpg 连接池 +
     to_thread(torch)」同存时，解释器退出会撞 `0xC000071C`（退出码 127）

5. 环境：PG 起着；测试 200 passed；后端单 worker 起在 8090。
   ⚠️ **跑测试或跑 CLI 之前先停掉后端**（Chroma 内嵌，A10）。

6. 规矩：提交只进 refactor/campus-rag，禁止动 main、禁止 force push。
   M3 做完停下汇报（交付 / 实测证据 / 偏差 / 已知问题），
   并把「完工交接」小节写进本文档、打 tag m3-done。

先给我一个 M3 的执行计划（任务顺序与各自怎么验），我确认后再动手。
```

### H.4 M4 开工提示词（当前待执行）

```
读 docs/superpowers/plans/2026-10-05-campus-rag-refactor.md，
重点读 §0（施工规程）、§8.9（M3 完工交接）与 §9（M4 原文 —— 那才是你要做的）。

我是这个项目的负责人，你接着做 M4（React 两端）。几条前提：

1. **M4 的形态又和前三轮不同：这一轮几乎全是「没有」，而且是前端为主。**
   下面是我开工前实测摸过的（不是照 §9.1 推断的），直接按它排计划：

   | §9.2 任务 | 代码实际状态 |
   |---|---|
   | M4-1 流式渲染 / 阶段提示 / 长列表 | ❌ **完全没有**。⚠️ 而且 `stage` 事件**服务端从来没发过**（grep 全后端零命中）—— 契约（`StageEvent` + §3.7.2 的取值表）M0 就冻住了，但**发送端没实现**。所以 M4-1 的前半段是**后端活**：先把 stage 下发补出来，前端才有东西可驱动 |
   | M4-2 管理端接口 | ❌ **完全没有**。`api/` 只有 auth / chat / documents（**仅上传**）/ document_access；**没有** users、admin（文档管理 CRUD、拒答分析）、stats。`services/` 里**没有** `stats_service.py` |
   | 会话列表（M4-1 后半段） | ⚠️ **半截**：`services/conversation_service.py` 的 `list_conversations` / `get_messages` 都写好了，但**接口层根本没建** —— 没有任何 `/api/conversations` 路由（只有一句注释提过它）。前端因此拿不到历史消息，刷新后引用角标无从渲染（§4.2.4） |
   | M4-3 管理端页面 + 仪表盘 | ❌ 完全没有。前端只有这些：`api/{client,sse,types}`、`components/DocumentDrawer`、`markdown/*`、`stores/auth`、`views/{login,chat}`。`react-router-dom` **已经在用**（`App.tsx` 有 `RequireAuth`，且已支持 `roles` 参数、`/admin/*` 留了占位注释）；**没有** ECharts、没有 shadcn/ui、没有 `api/schema.d.ts`（`gen:api` 脚本在，但 `openapi-typescript` 没装） |

   所以 M4 的量是：**三条后端链路（stage 下发 / 会话接口 / 管理与统计接口）
   + 一整块管理端前端**（布局、5 个页面、仪表盘、会话列表、流式优化）。

2. **开工第一件事：`uv run pytest backend/tests -q` 应是 297 passed**；PG 起着；
   后端当前**没在跑**。

3. ⚠️ **浏览器验收要一个能登录的 admin**：库里 `admin` 账号在，但 `.env` 里
   **没有 `ADMIN_PASSWORD`**（M1 按 D-5 删了）。M3 的做法是**建一个临时账号**、
   验收完删掉（连同它的会话与 qa_logs）；也可以用 `bsk request-help` 让我自己输口令。

4. ⚠️ **两个环境坑（M3 实测，会直接影响你的验收）**：
   - **GPU 被别的程序占满时，知识型提问会卡死**（不是代码问题）：`chat` 分支
     1.15 秒答完、LLM 自检也过，但走 rerank 的那条路一直不返回。定位手法：
     把 `reranker.model_path` 故意指错 → 同一问题 8.2 秒答完。
     **答辩前记得确认没有别的程序占 GPU。**
   - **bsk 的整页截图在 Agent 窗口被遮挡时会定格**（两次不同滚动位置 md5 相同），
     而 `bsk observe` 与元素裁剪仍是最新的 → 取证时开新标签页，或让我把窗口置前。

5. 施工注意事项见 §0.3（A1–A12）。最近两个里程碑又添了这些**实测**教训：
   - **别让中间层（Chroma / PG / 任何 get 类接口）的返回顺序决定最终排序**（M1 的 B-2）
   - **新增节点必须写 trace**，漏写会让 SSE 事件静默缺失（A3）
   - **兜底必须留痕**：静默兜底却不写 `degraded`，评测护栏就拦不住（M2 评审 I2）
   - **做对照实验时，两腿只能差一个变量**（M2 评审 I1）
   - **SSE 事件顺序有锁**（`tests/integration/test_refusal_paths.py`）：M4 要改
     `ChatPage` 的流式渲染，但**不要动服务端的事件顺序**
   - **前端 markdown 管道**：不要引入 `rehype-sanitize`（默认 schema 剥 `class`，
     置灰会静默失效）；两个 remark 插件的**顺序不能反**（置灰在前、角标在后）
     —— 有回归脚本守着（`frontend/web/scripts/annotation_probe/run.ts`，12/12）
   - **改 `api/` 照 A7 分层**：只做参数校验与响应封装，业务放 `services/`
   - **批处理入口（CLI）不要引入 `to_thread(torch)`**（Windows 退出码 127）

6. 环境：PG 起着；测试 297 passed；前端 `cd frontend/web && npx vite --port 5273`；
   后端单 worker 起在 8090。
   ⚠️ **跑测试或跑 CLI 之前先停掉后端**（Chroma 内嵌，A10）。

7. 规矩：提交只进 refactor/campus-rag，禁止动 main、禁止 force push。
   M4 做完停下汇报（交付 / 实测证据 / 偏差 / 已知问题），
   并把「完工交接」小节写进本文档、打 tag m4-done。

先给我一个 M4 的执行计划（任务顺序与各自怎么验），我确认后再动手。
```

---


> 约定：**每个里程碑都在新窗口里独立施工**，靠本文档交接，不靠上一轮的记忆。
> 下面这套流程是 M0 实跑一遍后总结的 —— M0 用了一个超长会话，
> 后半段明显感到设计约束在漂移，这就是要分窗的原因。

### 0.1 开工三步（新窗口第一件事）

1. **读本文档**（尤其 §0 与最近一个已完工的里程碑小节）。**不要重读 5665 行方案**——
   只有需要查设计意图时才回原文翻对应章节。
2. **读上一阶段的交接记录**：本文档里该里程碑的 `### 完工交接` 小节 + `git log --oneline -20`。
   commit message 里写了每个 bug 的**成因**，比只看代码省时间。
3. **确认环境**（见 §0.4），**尤其确认数据状态**——M0 就出现过「库里只剩 1 份语料，
   直接建基线会得到错的数」这种坑。

### 0.2 收工三步（里程碑完成时）

1. 把「本阶段交付 / 实测证据 / 偏差 / 已知问题」写成本文档里的 `### 完工交接` 小节
2. 提交 + 打 tag（`git tag mN-done`）+ 推送 `refactor/campus-rag`
3. 跑一遍全量测试，把测试数写进交接小节

### 0.3 施工注意事项（**每条都是 M0 实际踩过的坑**）

| # | 注意 | 踩过什么 |
|---|---|---|
| **A1** | **改动必须实测验证，不能只推理** | M0 有 15 个 bug 是实测抓出来的，其中至少 5 个推理绝对发现不了（如「向量路 12 次全降级但链路看着正常」） |
| **A2** | **文档的字面读法可能是错的** | 竖排恢复：文档只写「反转行序」，实测发现**必须同时合并各行**，否则成文日期被清洗规则吃掉。凡文档描述与实测冲突，**以实测为准并回写文档** |
| **A3** | **新增节点必须写 `trace`** | 漏写会让 SSE 的 `_node_ran()` 判 False，**事件静默缺失**（decisions/citations 全不发）。有回归锁 `test_graph_trace.py` |
| **A4** | **提示词里的 `$name` 是占位符，`{` 是普通字符** | 已改用 `string.Template`。**别退回 `str.format`** —— 会把 JSON 花括号当占位符，运行时才炸 `KeyError` |
| **A5** | **往 Chroma 写日期必须用整数 YYYYMMDD** | `$lte` 只吃 int/float。写成 ISO 字符串会让**整条向量路静默降级** |
| **A6** | **Chroma metadata 只能存标量** | 数组/对象存成 JSON 字符串；`collection.query` 与 `collection.get` 的返回**解析行为不同**，两路都要归一化（`search._json_field`） |
| **A7** | **分层：`api/` 只做参数校验与响应封装** | M0 一度把 246 行业务写进 `api/chat.py`，已重构。**新接口照 `services/` 放** |
| **A8** | **短锁必须与写操作同一事务** | `pg_advisory_xact_lock` 事务级，写成两次独立 execute = 锁在写之前就释放了 |
| **A9** | **改了 `filters.py` / `state.py` / 提示词，要跑全量测试** | 这三处被多处依赖，改动的爆炸半径大 |
| **A10** | **单 worker 是硬约束** | Chroma 内嵌，`--workers 1` 不能省。换了 PG 也不行 |
| **A11** | **破坏性操作（删数据/清索引）先归档、再问用户** | M0 清 `data/` 时被权限分类器拦下，这是对的 |
| **A12** | **提交只进 `refactor/campus-rag`，禁止动 main、禁止 force push** | 用户明令。main 是 L4 回滚的最后一道保险 |

### 0.4 环境与命令

```bash
# 基础设施
docker compose up -d --wait postgres          # PG 17-alpine，只绑 127.0.0.1

# 后端（单 worker 是硬约束）
cd backend && uv run uvicorn app.main:app --workers 1 --port 8090

# 前端
cd frontend/web && npx vite --port 5273        # 代理 /api → 127.0.0.1:8090

# CLI
cd backend && uv run python -m app.cli {init-db|create-admin|check-llm}

# 测试（从仓库根跑）
uv run pytest backend/tests -q

# 浏览器验收（bsk）
bsk daemon start --port 35000    # 端口必须显式 35000
bsk status                        # 确认 browsers connected ≥ 1
bsk session start --json
bsk session stop <id>             # 用完必须停，否则堵住命令队列
```

### 0.5 目录结构说明（与方案 §3.1 的对应）

```
backend/app/
├── core/       config logging telemetry security deps exceptions metrics(M5) prompts
│               └─ config/ 下另有 app.yaml(应用配置) security.yaml(仅环境变量名) prompts/*.txt
├── api/        只做参数校验与响应封装（A7）
├── services/   业务：chat_service document_service conversation_service
│               index_service qa_log_service stats_service(M4) eval_service(M5)
├── graph/      state builder + nodes/(11 个，已齐)
├── retrieval/  bm25 vector fusion reranker filters + search(编排) embedding
├── ingestion/  file_type chunker enrich versioning pipeline + loaders/ + mineru_client
└── schemas/    auth chat（conversation/document/eval 随对应里程碑补）
```

**未建目录的设计意图**：`api/{users,conversations,document_access,admin,eval}.py`
与前端 `router/ layouts/ views/admin/` 都是**按里程碑排期未到**，不是遗漏；
M3 补 `document_access`，M4 补其余，M5 补 `eval`。

---

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
- [x] `uv add asyncpg bm25s pyjwt`（实装 asyncpg 0.31.0 / bm25s 0.3.12 / pyjwt 2.15.1）；`uv add --force-reinstall python-pptx` → **实测 `import pptx` 成功、版本 1.0.2、`site-packages/pptx/` 真实存在**（B.1.1 的坏包装已修）
- [ ] **附录 C.2.2 依赖清理** —— ⚠️ **已从 M0-1 挪到 M0-8 之后**：C.2.2 要求"删完必须跑一遍所有支持格式"，而加载器到 M0-7 才有，现在清理**测不了回归**。清理内容：移除 `unstructured`/`markdown`/`openpyxl`/`aiofiles`/`langchain`(总包)/`python-magic` 六个；`modelscope` → `huggingface_hub`（改 `reorder_service.py:83`）；`pyproject.toml` 移除 `streamlit`（**不删 `front/`**，附录 A #10）
- [ ] ⚠️ 清理后**必须重跑支持格式回归**（txt/md/pdf/docx/pptx 各传一次）——**任何一项失败立刻回滚该依赖**
- [ ] ⚠️ 「删 modelscope 会让 `rapid_doc`(753MB) 消失」**因果链未经证实**，实测确认，**别当既成收益写进结论**
- [x] 写 `docker-compose.yml`（照抄附录 G），`docker compose up -d --wait postgres` → **实测 `healthy`**、PG **17.11**、端口确认只绑 `127.0.0.1:5432`
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

## 5.9 M0 完工时的实际状态（2026-10-05，**新会话先读这一节**）

> 本计划写在 M0 开工**之前**。M0 施工过程中发现：**M1 的代码交付物已经在做 M0 端到端时被顺带建出来了**。
> 照着下面原版的 M1 从零再建一遍是**纯浪费**。以下是与计划的偏差。

### M0 实际完成范围（11 个任务全做完，tag `m0-done`）

M0-0 数据归档清理 / M0-1 依赖+PG / M0-2 建表12张 / M0-3 日志+trace / M0-4 认证 /
M0-5 filters / M0-6 两层锁 / M0-7 加载层 / M0-8 摄入流水线 / M0-9 SSE / M0-10 图 / M0-11 前端

**验收已过**：bsk 驱动真实 Chromium 跑通「登录 → 提问 → 流式答案（含 `[1]`–`[5]` 句级引用）→ 引用面板 → 校验提示」。

### ⚠️ M1 的代码已存在 —— 不要重建

| 计划里的 M1 任务 | 实际状态 | 落在哪 |
|---|---|---|
| M1-1 BM25S + jieba + 下标→chunk_id 映射表 | **已实现** | `app/retrieval/bm25.py` |
| M1-2 向量过采样 + 一次重试 | **已实现** | `app/retrieval/search.py::vector_retrieve` |
| M1-3 版本折叠（回查 PG） | **已实现** | `search.py::latest_versions` / `fusion.py::fold_versions` |
| M1-4 加权 RRF | **已实现** | `app/retrieval/fusion.py::weighted_rrf`，在 `nodes/retrieve.py` 调用 |
| M1-5 精排 + 降级链 + GPU 信号量 | **已实现** | `app/retrieval/reranker.py` |
| ACL + 版本过滤 | **已实现** | `app/retrieval/filters.py` |
| 刷新令牌与登出语义 | **已实现** | `app/api/auth.py` |

**所以 M1 剩下的不是「施工」，是三件事**：

1. **补验收物**：20 题检索基线题库（`tests/fixtures/eval_min20.json`）+ Recall@5 / MRR 脚本；ACL 隔离集成测试
2. **补一个 M0 实测发现的缺口**：**reranker 启动预热**（见下）
3. **重新入库 10 份语料**（见下）

### M0 实测得出的、计划里没有的新事实

| # | 事实 | 影响 |
|---|---|---|
| N-1 | **reranker 冷启动要 23–80 秒**（页缓存热 23s / 冷 79.5s），加载后推理只要 0.4–3s | 必须加**启动预热**，否则第一个知识型提问要等一分多钟。这是当前最大的体验问题 |
| N-2 | **Chroma 的 `$lte` 只接受 int/float**，不接受字符串 | 已修（`filters.date_key`）；**任何往 Chroma 写日期的代码都必须用整数 YYYYMMDD** |
| N-3 | 节点漏写 `trace` 会让 SSE 的 `_node_ran()` 判 False，**导致事件静默缺失** | 已修 + 加了回归锁 `tests/integration/test_graph_trace.py`。**新增节点时必须写 trace** |
| N-4 | `str.format()` 会吃掉 prompt 里的字面 JSON 花括号 | 5 处已修；**新增 prompt 时花括号必须双写** |
| N-5 | Chroma metadata 的数组字段是 JSON 字符串，**两路（`query` 与 `get`）解析行为不同** | 已用 `search._json_field` 归一化 |

### 当前运行状态

```
PG       docker compose up -d --wait postgres    → campus_rag 库，12 张表
后端     cd backend && uv run uvicorn app.main:app --workers 1 --port 8090
前端     cd frontend/web && npx vite --port 5273   （代理 /api → 127.0.0.1:8090）
CLI      cd backend && uv run python -m app.cli {init-db|create-admin|check-llm}
测试     uv run pytest backend/tests -q            → 66 项
```

⚠️ **数据库里目前只有 1 份语料**（`09_应届毕业班学生参军入伍优待政策_试行.pdf`）。
M0 修 `$lte` 那个 bug 时清空过索引，只回填了 1 份用于验证。
**M1 建检索基线前必须把 `corpus/guet/` 的 10 份全部重新上传**（经 `/api/admin/documents/upload`，用 admin 账号）。

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

### 6.9 M1 完工交接（2026-10-05 实跑，**新会话先读这一节**）

> 口径按 §5.9：**M1 的检索代码 M0 已经建出来了，本轮不重建**。
> 实际做的是「重新入库 + 补验收物 + 补预热」三件事，
> 过程中实测又发现并修掉**三个缺陷** —— 计划里没写，但不修基线数字就不可信。

#### 本阶段交付

| # | 交付 | 落在哪 |
|---|---|---|
| 1 | **重新入库 10 份语料** | 148 chunk，PG 10 行 active/v1（详见下表） |
| 2 | **检索基线**（M1 验收①） | `backend/tests/fixtures/eval_min20.json` + `app.cli eval-retrieval` + `docs/检索基线.md` |
| 3 | **ACL 隔离集成测试**（M1 验收②） | `backend/tests/integration/test_acl_isolation.py` |
| 4 | **reranker 启动预热** | `app/main.py` lifespan + `app/retrieval/reranker.py` |
| 5 | 修 BM25 索引**三个缺陷** | `bm25.py` / `search.py` / `tests/support.py` |
| 6 | 仓库卫生：运行时派生产物移出版本库 | `.gitignore` + `git rm --cached` |

#### 实测证据（都能重跑复现）

| 项 | 证据 |
|---|---|
| 测试 | `uv run pytest backend/tests -q` → **86 passed**（M0 结束时是 66，本轮 +20） |
| 入库 | Chroma **148**；BM25 映射表 **148** / 落盘分词 **148** / token 为 0 的 **0 条** / 覆盖 **10 个 document_id**；PG 10 行 active v1 public |
| 基线 | `uv run python -m app.cli eval-retrieval` → **Recall@5 = 1.000（20/20）、MRR = 0.925**；单路对照：向量路 0.950 / BM25 路 1.000 |
| 预热 | 日志 `rerank.preloaded ok=true ms=14945`，同一时刻 `/health` 已 200；跑完一次真实提问后 `Loading CrossEncoder` 行数仍为 **1**（预热与请求共用同一次加载） |
| 端到端 | `桂电教〔2025〕28号这份文件是关于什么的？` → route=knowledge、decision=ANSWERED、引用命中**正确的第 03 份文档**、verify 2/2、**首字 5512ms**（预热前是 60s+） |
| ACL | student 两路都拿不到受限文档；**变异检验**：把 `build_where` 的 ACL 子句改成恒不生效 → 两个隔离用例立刻失败 |

#### 本轮实测发现并修掉的三个缺陷（计划里没有）

| # | 缺陷 | 怎么发现的 | 影响 |
|---|---|---|---|
| **B-1** | `bm25.add()` 重建索引时老文档分词结果只从**进程内缓存**取，进程重启即丢 → 老文档被写成空 token、**静默退出索引** | 开工前排查 + 受控实验（清缓存后 `add()`，老文档 BM25 分由 >0 变 0） | 现场：映射表 186 条 / 31 个 document_id 而库里只有 1 个文档，156 条 token 为 0，唯一现存文档的 6 个 chunk **全是 0** → 混合检索实际只有向量一路。**补充（评审时才发现）**：归档的 `params.index.json` 写着 `num_docs=12`，而映射表 186 条 —— bm25s 会**跳过空 token 的文档**，索引里只剩 12 篇，于是 `search()` 拿索引下标去查 186 条的映射表，**取回的是别人的 chunk_id**。所以 B-1 不只是「召回少」，是「返回错的 chunk」 |
| **B-2** | `bm25_retrieve()` 按 `collection.get()` 的返回顺序截断 Top-K，而 **Chroma 的 get 不保证按传入 ids 顺序返回** → BM25 算出来的排序被整条丢掉 | 写 ACL 正向对照时，某 chunk 以 4.69 分排第一却不在返回的 10 条里；逐段打点定位到 `folded[:k]` | 这一路返回的是「任意 K 条」。修复后 `桂电教〔2025〕28号` 的 BM25 top1 由 01_…2021-6号（3.18 分）变成正确的 03_…2025-28号（6.26 分） |
| **B-3** | 提权标记 `escalated` 只在 BM25 路写，**向量路漏了** | ACL 提权用例 | `Citation.escalated` 是 5.3 ACL 对照实验直接读的字段；只标一路则「哪些引用是越权拿的」有一半查不出来，而另一半看起来完全正常 |

> B-1 的成因还有一半在测试侧：`test_pipeline.py::cleanup_docs` **只删 Chroma 不删 BM25**，
> 每跑一次测试就往真实索引漏一批死条目（B-1 现场那 31 个 document_id 就是这么攒的）。
> 已一并修掉，并加了回归锁。

#### 与计划的偏差

| # | 计划口径 | 实际 | 依据 |
|---|---|---|---|
| D-1 | M1 剩三件事 | **四件事**：必须先修 B-1/B-2/B-3，否则基线数字偏低且无法解释 | 实测 |
| D-2 | 清索引只删那 1 份文档 | **整目录重建** `data/chromadb` + `data/bm25s` | BM25 索引已烂到无法增量修；Chroma 里只有那 1 份，等价 |
| D-3 | — | reranker 拆出**同步入口** `rerank_sync`，异步 `rerank()` 变成它的 `to_thread` 包装 | Windows 上 `asyncio.run` + `to_thread(torch/CUDA)` 在解释器退出时于 executor shutdown 撞 `0xC000071C`（退出码 127）。批处理走同步入口，服务端行为不变 |
| D-4 | — | `data/bm25s`、`data/uploads`、`data/normalized`、`logs/*.jsonl` **移出版本库** | M0 漏加 `.gitignore`，141 个派生产物被提交；不处理则每次重建索引都产生几 MB 二进制差异 |
| D-5 | 「开工前删掉 `.env` 的 ADMIN_PASSWORD」 | 改为**上传完成后立即删**（上传要用它登录） | 用户已同意 |

#### 完工评审（fresh reviewer，2026-10-05）

一次整分支复审（范围 `a18a0dd..c3be66b`）提出 4 条 Important，**已全部修掉**：

| # | 评审发现 | 修法 |
|---|---|---|
| R-1 | `bm25.add()` 把「磁盘上有索引但被判不可用」当成「还没有索引」从零重建 → **已有文档被静默清空**，且重建后 `is_available()` 立刻报健康。`INDEX_VERSION` 一升级就会踩到 | `add()` 在该情形下**直接抛错**（宁可让这次上传失败并留下可排查的 `failed` 任务，也不静默毁索引）。对应测试：`test_add_refuses_when_index_on_disk_is_unusable` |
| R-2 | 落盘索引与映射表可能**不同代**（两次写之间崩溃 / 第二个写者），`search()` 却把缓存的 `chunk_ids` 与重新读盘的索引配对 → 返回**别人的 chunk_id** | `_load_state()` 增加 `num_docs`（`params.index.json`）与 `len(chunk_ids)` 一致性校验。**这条护栏正好也能挡住 B-1 那个现场**（12 ≠ 186）。对应测试：`test_index_doc_count_mismatch_is_rejected` |
| R-3 | `eval-retrieval` 在精排降级时**照写基线文件**，产物里那张「链路含交叉编码器精排」的口径表就成了空话——而 M5 要拿它标定阈值 | 统计降级次数；有降级则**拒绝写文件**并退出码 1 |
| R-4 | 计划 Task M1-3 点名的**检索侧版本折叠测试**在仓库里根本不存在（只有写入侧的），§15 #1/#8 的检索半边也没覆盖 | 新增 `tests/integration/test_version_folding.py`（3 项）：v1 措辞更像 query 但必须返回 v2；两路折叠一致；未来生效日的 v2 不上线、v1 继续生效（§15 #8 的政策真空期） |

Minor 级 3 条**未修**，见下（K-8 / K-9 / K-10）。

#### 已知问题（**未修**，如实记录）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| K-1 | **06–10 五份公文的本文文号在源文件里就不存在**（06/07/10 的红头被裁掉了：页面高仅 595×358，是正常 A4 的 42%；**渲染确认像素里也没有**，不是提取丢的）。**01–05 正常**：文号就在 chunk 0 里，基线 5 题全 rank 1 命中 | 文号检索只对 01–05 有效。学生问「桂电学〔2019〕24号是什么」会答不上来 | **本轮决定不处理**（2026-10-05 用户定）：抽进 metadata 也**没有消费方**——要等 M2 规则层做「按文号直查」时它才有用。调查结论见下方「K-1 附记」，M2 直接取用 |
| K-2 | `bm25_retrieve` 在**无真实匹配**时仍返回 K 条 score=0 的 chunk（实测：某 query 的 BM25 top1 分为 0.00，返回的 10 条全是零分） | RRF 候选池混入噪声（由精排兜底，未观察到实际危害） | 可加「score ≤ 0 直接丢弃」。**本轮刻意不改**：计划未列此改动，改了会让基线反映一套未经确认的检索语义 |
| K-3 | `index_service.remove_from_index` 按 `range(removed_chroma)` 推断 chunk_id，**Chroma 已空时会把 BM25 条目留成孤儿** | 生产补偿删除路径的潜在孤儿源 | 本轮孤儿的主因是测试清理（已修）。该生产路径建议改成「先取 chunk_id 再删 Chroma」 |
| K-4 | `psql` **一次调用带两个 `-c`** 时，两条 DELETE 都报 `DELETE 0`（拆成两次调用立刻正常） | 运维陷阱：批量删数据会静默无效 | 操作时一次一个 `-c`；写进本节备查 |
| K-5 | 发现并停掉了**两个 M0 遗留的后端进程**（端口 8089 / 8091） | 它们持有内嵌 Chroma 的文件句柄，导致 `rm` 报 `Device or resource busy` | A10 说单 worker 是硬约束，同一份 Chroma 上挂三个服务本身就不该发生 |
| K-6 | `data/uploads`（70+）、`data/normalized`（65+）累积历史文件，测试每跑一轮还会再加 | 磁盘占用；已移出版本库，无功能影响 | 可加个清理脚本，不急 |
| K-7 | **前端 / UI 未回归** | M1 是后端里程碑，浏览器验证留给端到端深度测试 | 按 §11 走 |
| K-8 | **预热被取消并不会中断加载**：实测 `to_thread` 的工作线程照跑，`asyncio.run` 退出时会 join 它 → 冷启动窗口内 Ctrl+C / 重启，进程可能卡住最多一分钟 | 只是退出变慢，数据无不一致 | 可接受；若要改，得把预热改成可中断的分片加载 |
| K-9 | `tests/support.py::purge_documents` 的 `except Exception: pass` 把所有异常吞了 | 若 Chroma 被占用（本项目真遇到过），清理会整体静默失败并留下孤儿条目 | 至少该打一行日志 |
| K-10 | 基线的 ground truth 按**文档标题**匹配，无法区分版本 | 今天 10 份文档都是一组一版，无影响；一旦出现 v1/v2 同题，命中已废止版也会被算作命中 | M2/M5 重跑基线时改成按 `document_id`+`version` 匹配 |

#### K-1 附记：文号调查结论（2026-10-05 实测，**M2 要动文号时先读这条**）

这条不用重查，结论和判据都在下面：

- **不是提取问题**：把首页渲染成图看过 —— 红头（红色「桂林电子科技大学文件」+ 红线）与文号的位置**像素里就是空白**。这几份 PDF 是被裁掉了红头区域的版本，OCR 也救不了。
- **「本文文号」在正文里的首现偏移，能把 10 份干净地分成两组**：

  | 组 | 文档 | 首现偏移 |
  |---|---|---|
  | 有本文文号 | 01 / 02 / 03 / 04 / 05 | **5 – 14**（都在最开头） |
  | 没有 | 06(3460) / 07(1905) / 09(1232) / 10(337) | 这些位置出现的是**引用别人的**文号 |
  | 没有 | 08 | 全文 0 处 |

  → **判据取「只在开头 ~100 字符窗口内找」**，10 份全部分对。
  ⚠️ 朴素地在全文找第一个文号会把 06 判成「桂电教〔2016〕19号」、07 判成「桂电学〔2017〕12号」、10 判成「桂电教[2019]36号」—— 全是它们**引用**的别人的文号，**自信地答错比留空糟得多**。
- **06/07/10 的文号目前只有文件名这一个来源**：`桂电学2019-24号` / `桂电学2025-7号` / `桂电2021-2号`，**未经第二来源核对**。要补录必须先由人核对。
- **08/09 是否真有文号未知**（09 是党委发文，可能走党委文号体系）。
- **正文里的文号带空格或换行**（`桂电教〔2021〕6 号` 中间有空格；doc 04 里有跨行的 `（桂电学〔2025〕\n\n7 号`）→ 做精确串匹配前必须**归一化空白**。
- **将来若要修，两条路各有边界**（详见 §6.9 已知问题讨论）：
  - **补录进正文首行** → 同时解决「查文号」和「答文号」（反向问「《本科生管理规定》的文号是多少」需要文号出现在被检索的正文里）。注入必须在 `build_normalized_text` **之前**，否则踩 M0 那条「切片逐字等于 chunk 正文」的偏移回归锁。
  - **metadata 直查**（`doc_number` 列 + Chroma 冗余）→ 只解决「查文号」，但不受空格/换行/27号与28号混淆影响。**注意：只写 metadata 不写正文的话，反向问答仍然答不出。**

#### 下一里程碑开工前的提醒

- **M2 开工前必须先建 `eval_multiturn.json`**（计划 §7 验收要求，M2 开工前建好）
- 改 `filters.py` / `state.py` / 提示词要跑全量测试（A9）
- 新增检索节点/路径时记住 B-2 这类坑：**别让中间层（Chroma/PG）的返回顺序决定最终排序**
- K-1（文号）本轮**决定不处理**，理由是「没有消费方」；M2 要做规则层文号直查时，**先读上面的 K-1 附记**，别重新查一遍

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

### 7.9 M2 完工交接（2026-10-05 实跑，**新会话先读这一节**）

> 口径同 §5.9 / §6.9：**M2 的四个节点 M0 就写完了**，而且不是简实现 ——
> 闸门判据、jieba 分词代词匹配、省略三条、`resolve_skipped_reason` 分档、
> 路由三层收严 + `last_route` 只认 knowledge、clarify 兜底三级、
> rewrite 的 target/weight 服务端映射，**都在**。4 份提示词也都带注入防护。
>
> 所以本轮实际做的是：**建题库 → 建评测器 → 跑真实评测拿失败清单 →
> 补护栏测试 → 修实测缺陷**。计划 §7.2 的六条任务清单里，只有 M2-6
> （多轮指代题验收）是真的从零做，其余五条的代码都已存在。

> **tag 位置（刻意如此）**：`m2-done` 打在 **`506e848`**（完工评审通过的那一版）。
> 其后还叠着 K-1..K-5 的跟进提交 —— tag 钉住的是「被评审过的完整状态」，
> 回滚或追责时它指向的东西是验证过的；要看含跟进的最新状态就用分支头。
> 用户 2026-10-05 明确选择保持不动（而不是把 tag 强推到分支头）。

#### 本阶段交付

| # | 交付 | 落在哪 |
|---|---|---|
| 1 | **多轮指代题库**（15 段对话 / 27 轮，逐轮四标注） | `backend/tests/fixtures/eval_multiturn.json` |
| 2 | 题库完整性回归锁（字段 + ground truth 对活库/正文） | `tests/unit/test_eval_fixture.py`、`tests/integration/test_eval_fixture.py` |
| 3 | **评测器**（真实图 + 消解对照） | `app.cli eval-multiturn` → `docs/多轮指代评测.md` |
| 4 | 四个节点的护栏测试（§7.2 的测试点） | `tests/unit/test_{resolve,route,clarify,rewrite,prompts}.py` |
| 5 | 两条拒答路径的 SSE 事件序列锁 | `tests/integration/test_refusal_paths.py` |
| 6 | 修四处实测缺陷 + 提示词卫生 | 见下表 |

#### 实测证据（都能重跑复现）

| 项 | 证据 |
|---|---|
| 测试 | `uv run pytest backend/tests -q` → **198 passed**（M2 开工时 93，本轮 +105） |
| 验收跑 | `uv run python -m app.cli eval-multiturn` → **27/27 轮全绿，EXIT=0** |
| 指标 | 路由准确率 **1.000**；澄清误报率 **0.000**（0/25）；澄清命中率 **1.000**（2/2）；澄清漏报 **0** |
| 消解 | 消解命中率 **1.000**（7 个锚点轮） |
| **消解对照** | 消解后 Recall@5 **1.000** vs 不消解 **0.714**（两腿只差「消解」一个变量，下游同为 rewrite→检索→精排） |
| 路由来源 | 规则层命中 **25/27** —— 另 2 轮（mt-14/15）刻意避开制度名词与文号，**确实落到 LLM 路由且判对**（首轮 25/25 全被规则层拦住，这条端到端无证据，是评审提的缺口，已补） |
| 变异检验 | 改坏题库标题/锚点 → 2 条集成用例失败；让 `no_candidate` 路也发 `decision` → 拒答用例失败。均还原后通过 |

#### 本轮实测发现并修掉的四处缺陷（计划里没有）

| # | 缺陷 | 怎么发现的 | 影响 |
|---|---|---|---|
| **D-1** | 提示词的「三引号包裹历史」写成了**转义序列** `\"\"\"`（6 个提示词文件全中） | 写注入防护的断言时，断言 `'"""' in prompt` 失败 | 注入防护的分隔符实际下发的是 `\"\"\"` —— 防护还在，但不是文档要求的形式 |
| **D-2** | **纯符号输入触发无意义澄清**：「？？？」短且不含制度名词，正好踩中澄清触发条件二 | 评测题库 mt-13 首跑判成 clarify | 用户敲三个问号，被反问「你想问的是哪一项？」—— §15 #6 明令禁止。修法：把「没有实义字符」并入闸门判据（与 resolve 共用同一份） |
| **D-3** | 省略特征漏检「**几天**」「**多长时间**」 | 首跑 mt-04#2 / mt-10#2 的 `resolve_skipped_reason` 是 `no_feature` | 两轮静默跳过消解、原句直送检索。**当时两轮「恰好」都命中，但那是运气**，没有任何机制保障 |
| **D-4** | clarify 的 `facets` 为空数组时**没走固定问句兜底**（只有 question 为空才兜底） | 写 M2-3 的兜底测试时 | 与节点自己的兜底表、§7.2 M2-3 的「兜底三级」都不符 |
| 附带 | 提示词文件开头的 `#` 注释块**会被一起发给模型**，且注释里的 `$name` 让每次渲染都告警「缺少变量 ['name']」 | 首跑日志里每次都刷这条告警 | 注释白占 token；更要紧的是那条告警本是用来抓「真忘了传 `$query`」的，**天天响就等于不响** |

> D-2 / D-3 都是**探针题**先暴露、再定位到实现的：题库里那两个 `probe: true`
> 的轮次期望值由文档口径推得、不保证实现覆盖 —— 首跑两处全中，现在都转成常规题。

#### 与计划的偏差

| # | 计划口径 | 实际 | 依据 |
|---|---|---|---|
| E-1 | §7.2 的六条任务（四个节点填实） | 四个节点 M0 已写好；本轮做的是题库 + 评测器 + 护栏 + 修缺陷 | 读码实测（同 M1 的形态） |
| E-2 | 题库来自「5.1 第一阶段 60–80 题」 | 本卷 13 段/25 轮，只取**多轮指代型 + 路由/澄清负例** | 那 60–80 题是 M5 的交付物（§5.2）；M2 只自证「多轮指代题通过」 |
| E-3 | — | 评测器**精排走同步入口** `rerank_sync` | 实测：**asyncpg 连接池 + `to_thread(torch)` 同时存在**时，解释器退出撞 `0xC000071C`（退出码 127）。二分实测：只有池=0 / 只有 to_thread=0 / 池+Chroma+主线程 torch=0 / **池+to_thread=127**。这正是 §6.9 D-3 要避开的组合 |
| E-4 | — | 评测器在 `generate` **之前**截断 | M2 考的是查询理解；跑到底既多付一次 LLM 调用，又会把 M3 的缺陷混进 M2 的指标 |
| E-5 | — | 题库 mt-05#2 换过**三版** | 前两版都是标注没核到判据上，详见 `docs/多轮指代评测.md` 的「本卷口径修订记录」 |

#### 完工评审（fresh reviewer，2026-10-05）

范围 `m1-done..HEAD`。结论 **0 Critical / 4 Important**，四条已全部修掉：

| # | 评审发现 | 修法 |
|---|---|---|
| R-1 | **消解对照两腿差两个变量**（消解 + 查询扩展）→ 0.714 与 1.000 的差归因不到消解头上；而 M5 的消解消融很可能复用这套口径 | 对照腿也过 `rewrite_node`，两腿只差「消解」；报告口径同步改写 |
| R-2 | 评测护栏有**三处盲区**：route / clarify / rewrite 兜底时 `degraded` 恒为 None → 它们静默兜底的那一轮，指标照样被写进验收文档 | 三处兜底分支补 `degraded`（超时→`timeout`、其余→`unavailable`）+ 反向锁（正常路径不得标降级） |
| R-3 | 题库标签与实现**同 commit 联动修改**，验收文档无脚注 → 那两处「标什么跑什么」，不构成独立验证力 | 评测文档加「本卷口径修订记录」小节（由题库的 `revision_note` 生成）；mt-13 的口径判读写进本交接待签字 |
| R-4 | clarify 的**流式分支零覆盖** —— 而生产走的正是流式（`stream_mode` 带 `custom` 时 `get_stream_writer()` 是活的），单测全打在非流式兜底上 | 补 5 条流式用例（stub `llm.stream_raw` + 假 writer）：问句流式下发、facets 上限、流失败回落非流式、facets 空时刻意不套兜底 |

Minor 3 条已顺手修（`test_route` 里一句恒真断言、题库陈旧 note、本文件计数写错），其余见下「已知问题」。

#### 已知问题（**未修**，如实记录）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| K-1 | **`？？？` 的期望路由取 `chat` 是口径判读**：§15 #6 那行同时写了「不误判成闲聊」与「不触发无意义澄清」 | 只影响一个纯符号输入的落点 | **已由负责人签字确认**（2026-10-05）：按输入类别拆开读（纯符号↔不澄清、超长↔不误判闲聊），落 `chat`。题库 note 已同步 |
| K-2 | 非超时的解析失败也记 `skip_reason="timeout"` | 取值域只有五档，改它会动 §4.1 契约 | **取值域留给 M5**（维持原样）；**已修诊断文字**：评测器改从 trace 的 `degraded` 报数，不再把「解析失败」说成「超时」 |
| K-3 | 评测器喂给 resolve 的历史里，**助手回复恒为空串** —— knowledge 路径在 `generate` 之前截断，没有答案可放 | 本轮结果未受影响（消解只依赖用户侧上下文） | 维持原样，**M5 做消解消融时注意这条** |
| K-4 | ~~验收跑全被规则层拦住，「LLM 路由」端到端无证据~~ | — | **已补**：新增 mt-14/15 两题（避开制度名词与文号），实测落到 LLM 且判对，规则层命中变为 25/27 |
| K-5 | ~~库里一条测试残留卡在 `indexing`~~ | — | **已消**：该行被后续 ACL 测试的清理顺带删掉（库里现为 10 份 active、无残留）；**成因已修**：`tests/support.py::purge_documents` 不再 `except: pass`，清理失败会打日志（M1 的 K-9） |
| K-6 | 澄清的流式分支**刻意不套**「facets 空 → 固定问句」的兜底 | 与文档兜底表字面不一致 | 问句已逐字流给用户了，再换会让「看到的」与「存进历史的」变成两句话。已在代码与测试里注明 |

#### 下一里程碑开工前的提醒

- M3 要动 `build_context` / `generate` / `cite` —— 提示词改动**必须先跑全量测试**（A9），
  且 `test_prompts.py` 会锁占位符集合，新增 `$xxx` 要同步登记
- 拒答的两条路径已有 SSE 序列锁（`test_refusal_paths.py`），改 `chat_service` 的事件顺序会立刻失败
- 评测器与 `eval-retrieval` 都是**批处理入口**：Windows 上不要在里面引入
  `to_thread(torch)`（E-3 那个退出码 127 的组合）
- **K-1 文号直查**仍未做（M2 已拍板不做）；要做时先读 §6.9 的 K-1 附记，别重查一遍

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
- [ ] 测试点 A —— **重新写标注回归脚本**（负责人 2026-10-05 定）。
      原计划指的 `data/tmp/d6_probe/`（`make_input.py` + `annotate.mjs`）**不在本仓库、
      也从未进过 git 历史** —— 是旧项目产物，别去找。要**新写一个等价的**：
      - 覆盖 4.2.1 的**全部用例与三个漂移坑**（这就是它存在的理由：偏移映射出错时
        页面只是"标歪了"，肉眼很难发现，而脚本能一眼看红绿）
      - **必须 import 前端的真实实现**，不得把标注逻辑抄一份进脚本 ——
        抄一份的话脚本全绿而页面照错，等于白写
      - 每节点断言「源码切片 === `node.value`」；两道校验 A/B 都跑；
        叶子按 `value !== undefined` 收集；失败路径走「降级成消息级徽标」
      - 落在仓库内（如 `frontend/web/scripts/annotation_probe/`），可重复跑
      - 口径：**手工回归脚本，不是自动化测试**（文档 6.2 已定前端不做自动化测试）
- [ ] 测试点 B —— **用 bsk 驱动真实浏览器验收**端到端：
      **点引用真的跳到原文、无依据句真的被标出**。这两条只有在真浏览器里点得出来才算数。
      bsk 用法与三个坑见 §0.4 与附录 H.3 第 3 条
- [ ] 提交 `feat: 引用三层入口与原文抽屉`
- [ ] 打 tag `m3-done`，**停下汇报**

---

### 8.9 M3 完工交接（2026-10-05 实跑，**新会话先读这一节**）

> 口径同 §5.9 / §6.9 / §7.9：**一半代码有、一半完全没有**（开工前实测已列在附录 H.3）。
> 本轮实际做的是：三个节点的护栏 → 两条新链路（压缩 / 文件访问）→ 一整块前端（引用三层 + 置灰 + 抽屉）。
> 过程中实测抓出 **9 处缺陷**，其中 3 处是「不实测绝对发现不了」的类型。

#### 本阶段交付

| # | 交付 | 落在哪 |
|---|---|---|
| 1 | **上下文预算与滚动压缩**（M3-1） | `app/services/context_service.py`、`config/prompts/summarize.txt`、`state.summary`、`generate.txt` 加 `$summary` |
| 2 | **三个节点的护栏**（M3-2/3/4） | `tests/unit/test_build_context.py`(7)、`test_stream_json_parser.py`(18)、`test_cite.py`(21)、`test_prompts.py` 加四条硬约束 |
| 3 | **压缩的 DB 往返与硬约束** | `tests/integration/test_compaction.py`(8)、`tests/unit/test_token_budget.py`(12) |
| 4 | **原文访问接口 + 签名 URL**（M3-5） | `app/api/document_access.py`（`/file`、`/text`、`/images/{name}`）、`deps.current_user_optional`、`tests/integration/test_document_access_acl.py`(15) |
| 5 | **前端引用三层 + 置灰 + 抽屉**（M3-6） | `src/markdown/{citationPlugin,uncitedAnnotation,MarkdownAnswer}.tsx`、`src/components/DocumentDrawer.tsx`、`ChatPage.tsx` |
| 6 | **标注回归脚本**（§8.2 测试点 A） | `frontend/web/scripts/annotation_probe/run.ts`（`npx tsx` 跑，11 条） |
| 7 | 修 9 处实测缺陷 | 见下表 |

#### 实测证据（都能重跑复现）

| 项 | 证据 |
|---|---|
| 测试 | `uv run pytest backend/tests -q` → **292 passed**（M2 结束时 200，本轮 +92） |
| 摘要提示词 | 真模型跑通：146 字 / 94 token（上限 800），文号保留、`[n]` 已剥 |
| 前端构建 | `npm run build` 过；`dist/assets/pdf.worker.min-*.mjs` 1.36 MB —— **本地 worker 确实进了产物**（不是 CDN） |
| 标注回归脚本 | `npx tsx scripts/annotation_probe/run.ts` → **11/11** |
| **端到端①（点引用跳原文）** | bsk 真浏览器：点角标 → 抽屉打开 → 真 PDF 渲染 → 提示「**定位方式：坐标高亮（10 个框）**」，10 个高亮落在被引用的正文上（红头 / 文号 / 标题 / 各单位 / 正文 / 落款 / 页码） |
| **端到端②（无依据句标出）** | 问「学业预警分几级？每级有什么后果？」→ 末句「资料中未找到针对黄色、橙色、红色预警各自具体后果的进一步规定。」**标灰**（浅灰底 + 虚线 + 提示文案），上方另有「本回答含 1 处未证实内容」 |
| 图片缩略图 | 同一轮问答里，引用面板的校徽缩略图按文件名现取签名 URL 后**真实加载出图** |
| ACL 变异检验 | 把 `filters.can_access` 打成恒允许 → **6 条用例立刻变红**（证明用例有牙） |

#### 本轮实测发现并修掉的九处缺陷（计划里都没有）

| # | 缺陷 | 怎么发现的 | 影响 |
|---|---|---|---|
| **D-1** | 流式解析器把 **JSON 尾巴当正文**：`{"answer":…,"decision":…}` 时只在 `"`+`}` 收尾 | 写「decision 在后」的用例 | 答案里混进 `","decision":"ANSWERED`，并写进 `answer` —— cite/verify 拿这份文本算偏移，前端置灰跟着错 |
| **D-2** | **上游中断被当成拒答**，且 `error` 之后继续发事件 | 同上 | 客户端先收 `error` 再收 `refused`+`done`（违反「降级五条」第 ③ 条），把「模型输出坏了」记成「知识库没有依据」 |
| **D-3** | `precheck_prefix` 剥不掉「空白 → BOM → 围栏」 | 参数化用例 | 一条本来正常的流被误判成格式错误而中止 |
| **D-4** | **一轮问答的两条消息时间戳完全相同**（`now()` 是事务时刻，实测库里每个多轮会话 `distinct created_at = 1`） | 写压缩的消息顺序用例 | 历史可能以「助手在用户之前」的形式进提示词 |
| **D-5** | 标注「1 进 1 出」替换节点时**整段丢失**（只在节点数变化时才回写 children） | **回归脚本**坑③当场抓到 | `**30%**` 里的标注消失、外层照常 —— 看着像「只标了一半」 |
| **D-6** | **两个 remark 插件顺序反了**：角标插件拆出的文本节点没有 position，置灰插件排在它后面就永远标不上 | 浏览器实测（摘要说「含 1 处」而正文一个灰标都没有） | 含 `[n]` 的段落永远标不上灰 |
| **D-7** | 滚动锚点取到**页脚框**（Chroma 里 bbox 第一项常是页码行） | 浏览器实测（高亮全跑到视口外，y 为负） | 点引用打开后看不到高亮 |
| **D-8** | react-pdf-highlighter 默认 worker 是 **unpkg CDN**，取不到时**静默**退回主线程假 worker | spike 阶段翻 performance 资源列表 | 内网/答辩现场退化成主线程解析，页面看着正常 |
| **D-9** | `user=CurrentUser` 写成**默认值**导致依赖不解析 | 提权用例返回 404 | 提权分支永远走不到（Annotated 当默认值用，FastAPI 不解析） |

#### 与计划的偏差

| # | 计划口径 | 实际 | 依据 |
|---|---|---|---|
| E-1 | §8.1 把测试都放 `tests/unit/` | 按「要不要真库」拆成 unit + `integration/test_compaction.py` | 仓库既有惯例：unit 纯逻辑、integration 碰库 |
| E-2 | §8.2 M3-5 写「审计**表**有记录」 | 做的是**结构化审计日志**（谁/何时/哪份文档/trace_id） | 方案三处原文都写「审计日志」，且 12 张表里没有审计表；开工计划第 3 条已与你确认 |
| E-3 | §8.2 M3-1 的 B 路「真溢出时再裁检索上下文」 | **未实现那一档**：检索上下文已被 `build_context` 限死 8,000 token，对 100 万 token 的窗口裁它救不了任何东西。实现的是「先强制压缩历史 → 仍超则报 `context_length_exceeded`」 | 唯一可能无界增长的只有历史 |
| E-4 | 「测最新版 PDF 阅读器」 | 最新版就是 `8.0.0-rc.0`（2024-09 发布、至今没转正），实测可用 | 你指定的加载项 |

#### 完工评审（fresh reviewer，2026-10-06）

范围 `m2-done..HEAD`。结论 **0 Critical / 2 Important**，两条都已修掉；另修 6 条 Minor。
评审明确验证可靠的：文档访问三接口的鉴权/签名/防遍历、压缩的算术与保留区语义、
消息顺序、前端标注不误标（评审另跑了 9 个对抗用例，emoji / 重复句 / 行内代码 / ±1 漂移都没标错）。

| # | 评审发现 | 修法 |
|---|---|---|
| R-1 | **摘要模型返回空串 = 静默丢历史**：`llm.complete` 对空 content 不抛异常 → 走「成功」分支，`compressed_summary` 写成空串、`compressed_count` 照推 —— 那 N 条消息**永久消失**（库里只有一列摘要，旧摘要被就地销毁），且不记降级 | `_compact` 里空摘要**视为压缩失败**并抛错 → 不回写、不推进、记 `degradation_events`；补集成用例 |
| R-2 | **REFUSED 路径仍把模型文案流给用户**：token 边收边发，`finish()` 才换成固定话术 → 气泡里先渲染模型原话、下面再叠服务端拒答框，且与刷新后不一致（m2-done 就有，非 M3 引入） | 下发前判 `decision != REFUSED_NO_EVIDENCE`；补用例断言「拒答时一个 token 都不发」 |
| R-3 | `precheck_prefix` 只看首片：围栏被切成 `"```"` / `"```json"` 时误判失败 → 整轮中止 | 围栏还没换行收尾时返回「还判断不了」；补 3 条参数化用例 |
| R-4 | `force_compact` 无异常兜底（A 路有），异常穿透成 `error{internal}` | 加 try/except：拿原 prompt 继续，仍超硬限才报 `context_length_exceeded` |
| R-5 | 角标插件的 **1 进 1 出替换被丢弃**：整段只有 `[1]` 时一个 text 拆成一个 link、节点数不变 → 替换被跳过，角标渲染不出来 | 无条件回写 children（与 D-5 同一类错）；回归脚本补用例 |
| R-6 | 纯文本保底 token 把**拒答文案**也当正文补发 → 气泡渲染一遍、拒答框再渲染一遍 | 拒答路径不补发 |
| R-7 | `_SAFE_NAME` 用 `$` 会匹配结尾换行（不可利用，但没必要留着） | 换成 `\Z` |
| R-8 | 校验 B / `relocate_failed` 的降级**没人消费**（组件没传 report） | 组件收集 `AnnotationResult`，自检报错时摘掉插件重渲染（退回消息级徽标） |

> 评审同时记下一条**不改**的口径：A 路压缩反复失败时每轮都会重算一次摘要
> （最长 20 秒），这是 §3.8.3 明写的「不立刻重试，下一轮自然再触发」，不是缺陷。

#### 已知问题（**未修**，如实记录）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| K-1 | 「资料中未找到 X」这类**元陈述会被算成结论句**并标灰 | 灰标文案本身不算误导（那句确实没有依据），但属误报；方案 §3.5.3 的结论句定义里「元陈述」本应排除 | **留给 M5**：改它会动 5.2 的幻觉率口径，要用真实数据校准 |
| K-2 | L3 章节匹配依赖 `current_chapter`，实测 **125/148 非空** | 那 23 条 chunk 的引用会落到 L4（只跳页） | 可接受；要修先补章节抽取 |
| K-3 | 前端主包 **811 kB**（pdfjs 全家桶随包） | 首屏变慢 | M4 做长列表/性能时用 `React.lazy` 把抽屉拆出去 |
| K-4 | **bsk 整页截图在 Agent 窗口被遮挡时会定格**（两次不同滚动位置 md5 相同）；`bsk observe`/元素裁剪仍是最新的 | 只影响取证，不影响功能 | 验收时开新标签页，或让用户把 Agent 窗口置前 |
| K-5 | 测试跑一轮会在 `data/tmp/`、`data/uploads` 累积文件 | 磁盘占用 | 已有 gitignore（K-6 同 M1） |
| K-6 | 本轮验收用的截图留在 `data/tmp/`（已 gitignore） | 无 | 需要证据时直接看，不必重跑 |
| K-7 | **提权在 UI 上是死代码**：前端从不发送 `include_restricted`（对话请求与抽屉都没这个开关） | admin 在页面上看不到受限文档；后端行为本身正确且有 15 条用例 | 归 M4（管理端/权限开关一起做）；本轮只把抽屉里「下载原文」漏带的提权标记补齐 |
| K-8 | 可压区不足 6 条时会并入 1–5 条 | 与 §3.8.3 约束 ① 的字面口径略有出入（多一次小批量摘要调用） | 刻意如此：不压的话历史会卡在预算上方永远下不来；已在代码里注明 |

#### 下一里程碑开工前的提醒

- M4 要动 `ChatPage` / 流式渲染 —— **不要改 `generate`/`cite` 的 SSE 事件顺序**（`test_refusal_paths.py` 有锁）
- 前端 markdown 管道**不要引入 `rehype-sanitize`**：默认 schema 会剥 `class`，置灰**静默失效**
- 两个 remark 插件的**顺序不能反**（置灰在前、角标在后），有回归脚本守着
- `count_tokens` 只有一份（`services/context_service.py`），别在别处再写一个
- 单 worker 仍是硬约束（A10）；跑测试/CLI 前先停后端

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
| R12 | 前端不做自动化测试（文档已定） | — | 中 | **改用 bsk 驱动真实浏览器验收**（2026-10-05 定 —— 原 `data/tmp/d6_probe/` 脚本不在本仓库）；验收结果写进交接 |

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
