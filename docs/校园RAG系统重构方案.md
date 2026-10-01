# 校园 RAG 检索问答系统 — 重构方案

> 版本：v1.0 ｜ 日期：2026-10-01 ｜ 状态：待审批

---

## 一、项目定位

### 1.1 三个目标

| 目标 | 含义 |
|---|---|
| 毕业设计 | 七个模块功能完整，可演示、可讲清 |
| 面试作品 | 有 3–4 个能拿出数据和对比实验的深度点 |
| 上线级质量 | 架构、测试、可观测性按生产标准做，**但不真上线** |

**不做的事**：不对接学校统一认证、不做真实流量压测、不建运维体系。

### 1.2 决策清单

后续所有设计均以此为准，改动需重新评估影响面。

| # | 决策项 | 结论 |
|---|---|---|
| 1 | 推进方式 | **全量重写**（现有代码作参考，不直接改造） |
| 2 | 前端 | Vue 3 单工程双端：User 问答端 + 管理端 |
| 3 | 编排 | LangGraph；**废弃现有 Agent 模式**，只保留线性 RAG |
| 4 | 权限 | 真实 ACL，**检索期数据隔离**（非仅接口鉴权） |
| 5 | 版本管理 | 版本号 + 生效日期 + 新版自动取代旧版 |
| 6 | 文档格式 | txt / pdf / md / pptx / docx（**不做 HTML**） |
| 7 | 架构顺序 | 指代消解前置到 Query Router 之前 |
| 8 | 策略 | 4 个亮点做深，其余模块够用即可 |

### 1.3 四个亮点

面试与答辩的核心谈资，需做到有数据支撑：

| # | 亮点 | 深度所在 |
|---|---|---|
| ① | **检索期 ACL + 版本过滤** | 多数 RAG 项目只做接口鉴权，做到检索层数据隔离的很少 |
| ② | **混合检索消融实验** | 用 ragas 量化证明分词、RRF、精排每一步的增益 |
| ③ | **引用溯源到原文回跳** | 精确定位页码 + 章节 + 字符偏移 |
| ④ | **机制化拒答** | 条件边拦截，证据不足时根本不调用大模型 |

### 1.4 验收标准

- 端到端：上传文档 → 提问 → 流式作答 → 引用可点击跳原文
- 检索：消融实验表产出，各阶段增益有数据
- 安全：测试证明 A 角色无法检索到 B 角色的受限文档
- 质量：核心模块有单元测试与集成测试，ragas 指标有基线
- 运维：结构化日志、节点级耗时追踪、降级事件可查

---

## 二、总体架构

```
                        ┌─────────────────┐
                        │   Vue Console   │
                        │  User端 | 管理端 │
                        └────────┬────────┘
                                 │ HTTPS / SSE
                        ┌────────▼────────┐
                        │     FastAPI     │
                        └────────┬────────┘
                                 │
                        ┌────────▼────────┐
                        │ Security Context│  JWT → 用户身份 + 角色
                        └────────┬────────┘
                                 │
                 ┌───────────────▼───────────────┐
                 │        LangGraph 编排          │
                 │                               │
                 │  resolve  指代消解 + 语义补全    │
                 │     ↓                         │
                 │  route    三分类路由            │
                 │  ┌──┼──────────┐              │
                 │  ↓  ↓          ↓              │
                 │ chat clarify  rewrite (HyDE)  │
                 │  ↓  ↓          ↓              │
                 │ END END    ACL + 版本过滤       │
                 │            ┌───┴───┐          │
                 │            ↓       ↓          │
                 │          BM25    Vector       │
                 │            └───┬───┘          │
                 │               RRF             │
                 │                ↓              │
                 │             Rerank            │
                 │                ↓              │
                 │          ┌── 证据够? ──┐       │
                 │          ↓             ↓      │
                 │     Context Builder  Refuse   │
                 │          ↓             ↓      │
                 │      Generator        END     │
                 │          ↓                    │
                 │      Citation                 │
                 │          ↓                    │
                 │         END                   │
                 └───────────────────────────────┘
                                 │
                 ┌───────────────┼───────────────┐
                 ↓               ↓               ↓
            ┌────────┐    ┌──────────┐   ┌───────────┐
            │ SQLite │    │  Chroma  │   │  BM25S    │
            │ 结构化  │    │  向量库   │   │  稀疏索引  │
            └────────┘    └──────────┘   └───────────┘
```

**三条存储分工明确**：

| 存储 | 存什么 | 为什么 |
|---|---|---|
| SQLite | 用户、文档元数据、会话、问答日志 | 需事务、需 join、需统计聚合 |
| Chroma | chunk 文本 + 向量 + 过滤字段 | 语义检索 |
| BM25S | 稀疏索引（磁盘持久化） | 关键词精确匹配 |

---

## 三、后端设计

### 3.1 工程结构

```
backend/
├── app/
│   ├── main.py                  FastAPI 入口、中间件、路由注册
│   ├── core/
│   │   ├── config.py            配置加载（YAML + .env，禁止硬编码密钥）
│   │   ├── logging.py           结构化日志
│   │   ├── security.py          JWT 签发/校验、密码哈希
│   │   ├── deps.py              依赖注入：current_user、require_role
│   │   ├── exceptions.py        统一异常 + 全局处理
│   │   └── metrics.py           节点耗时、降级事件、检索指标采集
│   ├── api/
│   │   ├── auth.py
│   │   ├── chat.py              User 端问答（SSE）
│   │   ├── conversations.py
│   │   ├── documents.py         管理端文档
│   │   ├── admin.py             仪表盘统计
│   │   └── eval.py              评测触发与查询
│   ├── schemas/                 Pydantic 契约（前后端共同依据）
│   ├── graph/                   LangGraph 编排
│   │   ├── state.py             RAGState 定义
│   │   ├── builder.py           图装配
│   │   ├── checkpointer.py      SqliteSaver 配置
│   │   └── nodes/               见 3.5
│   ├── retrieval/
│   │   ├── bm25.py              BM25S + jieba
│   │   ├── vector.py            Chroma 封装
│   │   ├── fusion.py            RRF
│   │   ├── reranker.py          Cross-Encoder
│   │   └── filters.py           ACL + 版本过滤条件拼装
│   ├── ingestion/
│   │   ├── loaders/             pdf / docx / pptx / md / txt
│   │   ├── chunker.py           分块
│   │   ├── enrich.py            元数据富化
│   │   ├── versioning.py        版本状态机
│   │   └── pipeline.py          摄入主流程
│   ├── services/
│   │   ├── chat_service.py
│   │   ├── document_service.py
│   │   ├── conversation_service.py
│   │   ├── stats_service.py     仪表盘聚合
│   │   └── index_service.py     BM25/向量索引的构建与失效
│   └── prompts/                 提示词模板
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```

**分层规则**：`api/` 只做参数校验与响应封装，不写业务；业务在 `services/`；`graph/nodes/` 只做编排逻辑，检索细节委托给 `retrieval/`。

---

### 3.2 基础设施层

#### 3.2.1 配置管理

```
config/
├── app.yaml        应用配置（分块参数、检索参数、阈值）
├── security.yaml   密钥引用（只存环境变量名，不存值）
└── prompts/        提示词
```

**硬性要求**：密钥一律走环境变量。现有项目中 MinerU 密钥硬编码在 `app/config/chroma.yaml` 且已提交进 git 历史 —— 新项目必须杜绝，且**旧密钥需吊销重发**。

#### 3.2.2 安全上下文

```
HTTP 请求
   ↓
Authorization: Bearer <JWT>
   ↓
core/security.py  解码校验
   ↓
core/deps.py      构造 UserContext
   ↓
{ user_id, username, role, visible_scopes }
   ↓
注入到接口函数 + 存入 RAGState
```

| 角色 | 权限 |
|---|---|
| `student` | User 端问答、查看自己的会话 |
| `staff` | 同 student |
| `admin` | 全部 + 管理端所有操作 |

**关键约束**：角色与身份**只能来自 JWT**，任何接口都不得接受客户端传入的 `user_id`。这是现有项目最大的安全缺陷（现在传谁的 id 就能读谁的资料）。

#### 3.2.3 可观测性

| 类型 | 落地方式 | 用途 |
|---|---|---|
| 结构化日志 | JSON 格式，含 trace_id / session_id / node | 出问题能按会话串联 |
| 节点耗时 | 每个 LangGraph 节点自动记录耗时与召回数 | 仪表盘"检索性能"数据源 |
| 降级事件 | 重排超时、BM25 索引失效等 | 仪表盘可见，面试可讲 |
| 问答日志 | `qa_logs` 表 | 评测与统计的原始数据 |

---

### 3.3 数据层

#### 3.3.1 SQLite 表结构

**`users`**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| username | TEXT UNIQUE | |
| password_hash | TEXT | bcrypt |
| role | TEXT | student / staff / admin |
| created_at | DATETIME | |

**`documents`** — 核心表

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| **doc_group_id** | TEXT | 同一制度的不同版本共享，用于版本串联 |
| title | TEXT | 文档标题 |
| filename | TEXT | 原始文件名 |
| file_type | TEXT | pdf / docx / md / pptx / txt |
| md5 | TEXT | 文件内容指纹，用于去重 |
| **version** | INTEGER | 版本号，同组内递增 |
| **effective_date** | DATE | 施行日期 |
| **status** | TEXT | active / superseded / disabled |
| **visibility** | TEXT | public / restricted |
| **visible_roles** | TEXT(JSON) | restricted 时生效，如 `["admin"]` |
| uploader_id | TEXT FK | |
| chunk_count | INTEGER | |
| created_at / updated_at | DATETIME | |

**状态机**：

```
      上传新版本文档
            ↓
   ┌────────────────┐
   │ 旧版(同组其它)  │
   └───────┬────────┘
           ↓
    status: active → superseded   （自动，不可逆除非手动）

             管理员手动操作
                   ↓
        active ⇄ disabled
```

| 状态 | 是否参与检索 |
|---|---|
| `active` | ✅ 是（还需满足生效日期条件） |
| `superseded` | ❌ 否（保留供管理端查看历史） |
| `disabled` | ❌ 否（管理员手动停用） |

**`conversations`** / **`messages`** — 沿用现有设计，字段不变。

**`qa_logs`** — 新增

| 字段 | 说明 |
|---|---|
| id / session_id / user_id / role | 归属 |
| question | 原始问题 |
| resolved_query | 消解补全后 |
| route | chat / clarify / knowledge |
| retrieved_chunk_ids | JSON 数组 |
| reranked_chunk_ids | JSON 数组 |
| answer | 最终答案 |
| **is_refused** | 是否拒答 ← 拒答率统计的数据源 |
| refusal_reason | 证据不足 / 无命中 |
| latency_ms | 总耗时 |
| node_timings | JSON，各节点耗时 |
| created_at | |

#### 3.3.2 向量库 metadata 设计

Chroma 的 chunk metadata **必须冗余存一份过滤字段**：

```
document_id, doc_group_id, status, effective_date,
visibility, visible_roles, current_chapter, chapter_level,
page, page_start, page_end, image_paths
```

**为什么要冗余**：Chroma 只能按自身 metadata 过滤，无法 join SQLite。若改为"先查 SQLite 拿合法文档 ID，再用 ID 列表查 Chroma"，文档一多该列表会超出查询上限。

#### 3.3.3 检索期过滤条件（亮点①）

```
属于该知识库
  AND status = "active"                    ← 版本过滤
  AND effective_date <= 今天                ← 生效日期过滤
  AND (visibility = "public"
       OR 用户角色 ∈ visible_roles)         ← ACL
```

#### 3.3.4 后过滤召回不足问题（面试深度点）

**问题**：Chroma 是「先按向量相似度取 Top-N，再按 metadata 过滤」。若 Top-N 中大部分被权限过滤掉，实际可用结果可能只剩一两条——而库中明明存在相关内容，只是没进 Top-N。

**方案**：按过滤严格程度**动态放大召回数**。

```
召回数 = 基础召回数 × 放大系数

放大系数由过滤条件推导：
  仅 status 过滤            → 1.5
  + 生效日期过滤            → 2.0
  + ACL 角色过滤            → 3.0
  过滤后不足基础召回数      → 继续翻倍重试（上限 3 轮）
```

最终对过滤后的结果**重新截断**到标准 Top-K。此过程记入 `qa_logs.node_timings`，可在仪表盘观察放大触发频率。

---

### 3.4 文档摄入模块

#### 3.4.1 主流程

```
上传（单文件 / zip 批量）
        ↓
  格式校验（扩展名 + MIME + 文件头）
        ↓
  MD5 计算
        ↓
  ┌─ 已存在? ─┬─ 是 → 标记 duplicate，跳过
  │           │
  │           └─ 否 ↓
  ↓
  内容解析（按格式分发）
        ↓
  文本清洗（控制字符 / 页眉页脚 / 目录行）
        ↓
  分块（500 字 / 50 重叠 / 中文标点切分）
        ↓
  元数据富化（章节、页码、图片路径）
        ↓
  ┌─ 版本判定 ────────────────────────┐
  │ 是否同 doc_group 已有文档？        │
  │  是 → version +1，旧版 → superseded│
  │  否 → 新建 group，version = 1      │
  └───────────────┬───────────────────┘
                  ↓
        批量向量化（20 chunk / 18000 字一批，指数退避重试）
                  ↓
        ┌─────────┼─────────┐
        ↓         ↓         ↓
     Chroma    BM25S     SQLite
     写向量    增量索引   写元数据
        └─────────┴─────────┘
                  ↓
            记录 MD5 指纹
```

#### 3.4.2 加载器

| 格式 | 实现 | 说明 |
|---|---|---|
| PDF | 三路分支 | 纯文本 / 图文混排 / 扫描件（走 OCR），沿用现有成熟实现 |
| DOCX | python-docx | 提取标题层级 + 内嵌图片 |
| PPTX | python-pptx | 按页提取 |
| Markdown | mistune AST | 提取目录结构 |
| TXT | 编码回退链 | UTF-8 / GBK / GB18030 依次尝试 |

**HTML 不做**（决策 #6）。

#### 3.4.3 版本判定（决策 #5）

```
新文档上传
    ↓
按 (title + 归属知识库) 匹配已有文档组
    ↓
┌── 匹配到 ──┐          ┌── 未匹配到 ──┐
↓            ↓          ↓              ↓
version =   同组旧版     新建 group
  旧版+1     → superseded  version = 1
↓            ↓          ↓
沿用同一 doc_group_id
    ↓
录入 effective_date（上传时指定，默认今天）
```

**增量更新**：MD5 相同 → 直接跳过；MD5 不同但同组 → 按新版本处理。**不做 chunk 级 diff**（决策 #5 已明确）。

#### 3.4.4 索引一致性

三处存储（SQLite / Chroma / BM25S）必须同步。策略：

- **写入顺序**：Chroma → BM25S → SQLite（SQLite 最后，作为"已完成"的标记）
- **失败处理**：任一步失败则整体回滚，记录到失败队列，管理端可见并可重试
- **删除顺序**：先删索引，后删元数据

---

### 3.5 LangGraph 编排

#### 3.5.1 状态定义

所有节点读写同一个 `RAGState`：

```python
class RAGState(TypedDict):
    # 输入
    query: str
    session_id: str
    user: UserContext            # id / role / visible_scopes

    # 查询理解
    history: list[Message]
    resolved_query: str
    skipped_resolve: bool        # 是否跳过了消解（供观测）
    route: str                   # chat | clarify | knowledge
    clarify_question: str

    # 检索
    rewritten_queries: list[str]
    candidates: list[Chunk]      # RRF 融合后
    reranked: list[Chunk]        # 精排后
    retrieval_confidence: float

    # 生成
    context: str
    answer: str
    citations: list[Citation]
    refused: bool
    refusal_reason: str

    # 可观测
    trace: list[NodeTrace]       # 各节点耗时、召回数、降级事件
```

#### 3.5.2 图拓扑

```
START
  ↓
resolve ──────────────── 指代消解 + 语义补全
  ↓
route ────────────────── 三分类（条件边）
  ├── chat     → END
  ├── clarify  → END
  └── knowledge
        ↓
      rewrite ────────── HyDE 改写
        ↓
      retrieve ───────── 并行 BM25 ∥ Vector → RRF
        ↓
      rerank ─────────── Cross-Encoder 精排
        ↓
      assess ─────────── 证据充分性判定（条件边）
        ├── 充足 → build_context → generate → cite → END
        └── 不足 → refuse → END
```

**三个关键设计**：

1. **拒答做成条件边**（亮点④）：`assess` 节点判定证据不足时直接走 `refuse` 分支，**完全不调用大模型**。既省成本，又从机制上杜绝"证据不足仍硬编"。
2. **检查点即记忆**：使用 LangGraph 自带 `SqliteSaver`，多轮状态、断点续跑由框架保证，无需自建消息表。
3. **resolve 可跳过**（决策批准项 3）：无历史对话，或问题无代词/省略且长度足够时直接透传，闲聊场景不浪费 LLM 调用。

#### 3.5.3 节点设计

---

##### 节点 1：`resolve` — 指代消解与语义补全

**职责**：把「它需要什么条件？」这类依赖上下文的问题，补全为自包含的完整问题。

**结构图**

```
输入 query + history
        ↓
  ┌─────────────┐
  │ 需要消解吗？ │
  │ · 有历史？   │
  │ · 含代词？   │
  │   它/这个/那 │
  │ · 有省略？   │
  │ · 长度够吗？ │
  └──┬───────┬──┘
  否 │       │ 是
     ↓       ↓
   透传   LLM 消解补全
     └───┬───┘
         ↓
  resolved_query
```

**设计要点**

| 项 | 方案 |
|---|---|
| 触发条件 | 有历史 AND（含代词 OR 长度 < 阈值） |
| 模型 | 分类用小模型（低温度），补全用主模型 |
| 失败处理 | LLM 超时 → 透传原 query，标记 `skipped_resolve` |
| 观测 | 记录是否跳过，供仪表盘统计跳过率 |

---

##### 节点 2：`route` — 三分类路由

**结构图**

```
resolved_query
      ↓
  LLM 三分类
（带少量示例的提示词）
      ↓
 ┌────┼─────────┐
 ↓    ↓         ↓
Chat Clarify Knowledge
 ↓    ↓         ↓
END  END    继续检索链路
```

**设计要点**

| 类别 | 判定 | 去向 |
|---|---|---|
| `chat` | 问候、寒暄、与知识库无关 | 直接生成简短回复 |
| `clarify` | 意图模糊、指代无法消解、问题过于宽泛 | LLM 生成反问，引导明确意图 |
| `knowledge` | 需要查知识库 | 进入检索链路 |

- **低置信度兜底**：分类置信度低于阈值时一律归入 `knowledge`（宁可多检索，不可漏答）
- **澄清回路**：用户回答澄清问题后，新问题重新进入图（不保留澄清状态，由历史承担）

---

##### 节点 3：`chat` — 普通问候

**结构图**

```
resolved_query → 短提示词 → 流式生成 → END
```

极简节点。**不检索、不引用、不加"📚参考来源"**。

---

##### 节点 4：`clarify` — 澄清反问

**结构图**

```
resolved_query + history
        ↓
  LLM 生成反问（引导明确意图）
        ↓
  流式输出 + 标记 route=clarify
        ↓
       END
```

反问需满足：**具体到可选项**（"你是想问缓考申请条件，还是缓考的考试安排？"），而非泛泛的"请详细说明"。

---

##### 节点 5：`rewrite` — 查询重写（HyDE）

**结构图**

```
resolved_query
      ↓
  ┌───────┴────────┐
  ↓                ↓
HyDE 生成        策略判定
假设答案         纯关键词？
  │                │
  └───────┬────────┘
          ↓
  rewritten_queries: list[str]
```

**设计要点**

| 策略 | 触发 | 检索侧重 |
|---|---|---|
| `bm25_only` | 纯关键词（文号、短专有名词） | 只走 BM25 |
| `hybrid` | 常规问题 | 双路 |
| `hybrid_rewritten` | 口语化、表述鸿沟大 | 双路 + HyDE 扩写 |

HyDE 生成的假设答案**用于向量检索**（补足口语化问题与正式文本的鸿沟），原始 query **用于 BM25 检索**（保留关键词精度）。

---

##### 节点 6：`retrieve` — 混合检索

**结构图**

```
resolved_query + rewritten_queries
          ↓
    ┌─────┴──────┐        并行执行
    ↓            ↓
┌────────┐  ┌──────────────┐
│  BM25  │  │    Vector    │
│        │  │              │
│ jieba  │  │ 过滤条件拼装  │
│ 分词   │  │ (ACL+版本)   │
│        │  │      ↓       │
│ BM25S  │  │ 动态过采样    │
│ 索引   │  │ 召回 → 过滤   │
└───┬────┘  └──────┬───────┘
    │              │
    └──────┬───────┘
           ↓
        RRF 融合
           ↓
      candidates
```

**BM25 改造**（批准项 1）

| 项 | 现状 | 改造后 |
|---|---|---|
| 分词 | `str.split()` —— **中文完全失效** | `jieba.lcut` |
| 库 | rank_bm25（内存、每次全量重建） | **BM25S**（稀疏矩阵、磁盘持久化、快 100+ 倍） |
| 更新 | 无 | 增量索引 + 索引失效检测 |

**过滤条件**（亮点①，见 3.3.3）：拼装后同时作用于向量检索与 BM25 索引构建。

**动态过采样**（批准项 4，见 3.3.4）。

**RRF 融合**

```
score(d) = Σ  1 / (k + rank_i(d))
          i∈[BM25, Vector]

k = 60（可配置，消融实验的调参对象）
```

融合去重键：`chunk_id`。

---

##### 节点 7：`rerank` — 交叉编码器精排

**结构图**

```
candidates (RRF 融合后，约 20-30 条)
        ↓
  Cross-Encoder (BGE-reranker-v2-m3)
        ↓
  ┌─── 超时/显存不足? ───┐
  │ 是 → 降级：直接用 RRF 结果  │
  │      记降级事件             │
  └───────────┬─────────────┘
              ↓
        reranked (Top-K)
+ retrieval_confidence = 最高分
```

**降级策略**（必须有，否则 GPU 一挂整个问答失败）：

| 异常 | 处理 |
|---|---|
| 推理超时（> 阈值） | 跳过精排，用 RRF 顺序 |
| 显存不足 | 降级到 CPU，或跳过 |
| 模型加载失败 | 跳过精排 |
| 以上任一 | 记录降级事件到 `trace`，仪表盘可见 |

---

##### 节点 8：`assess` — 证据充分性判定（亮点④）

**结构图**

```
reranked + retrieval_confidence
          ↓
  ┌───────┴────────┐
  │ 证据充分吗？     │
  │ · 最高分 ≥ 阈值？│
  │ · 有效片段 ≥ N？ │
  └───┬─────────┬──┘
   充足│         │不足
      ↓         ↓
 build_context  refuse
```

**设计要点**

- **纯规则判定，不调用 LLM** —— 这是"机制化"而非"提示词祈祷"的关键
- 阈值来自评测集调优（M5 阶段确定），写入配置
- 判定结果与依据分数一并记入 `qa_logs`，可回溯

---

##### 节点 9：`build_context` — 上下文组装

**结构图**

```
reranked (Top-K)
      ↓
按文档分组 → 组内按 chunk_index 排序
      ↓
拼装：文档名 / 章节 / 页码 / 正文 / 图片占位
      ↓
裁剪到 token 预算（超出则从低分片段裁）
      ↓
   context
```

**关键要求**：每段证据必须携带**完整溯源信息**（文档 ID、chunk ID、页码、字符偏移），供 `cite` 节点生成引用。这是亮点③的基础。

---

##### 节点 10：`generate` — 答案生成

**结构图**

```
context + resolved_query + 提示词
          ↓
      主模型
          ↓
   流式输出 token ──→ SSE ──→ 前端逐字渲染
          ↓
       answer
```

**提示词硬约束**

1. 仅依据给定材料作答
2. 材料不含答案时明确说明（**双保险**：`assess` 已拦一道，此处再声明一道）
3. 不得编造文号、日期、数字
4. 引用格式固定，便于 `cite` 节点解析

---

##### 节点 11：`cite` — 引用计算

**结构图**

```
answer + reranked + chunk 溯源信息
          ↓
  ┌───────┴────────┐
  │ 哪些 chunk 被用到了？│
  │ · 答案文本与 chunk 相似度？│
  │ · LLM 显式标注？         │
  └───────┬─────────┘
          ↓
   citations: [
     { document_name, chapter, page,
       snippet, chunk_id,
       jump_target }      ← 原文回跳定位
   ]
          ↓
  随 SSE 下发 + 存入消息记录
```

**统一引用机制**（解决现有项目"代码算一份、LLM 写一份"打架的问题）：

- **以代码计算为准**，唯一来源
- 只输出**答案中实际使用**的 chunk 的引用，而非全部检索结果
- 前端只渲染这一份

**`jump_target` 构成**：文档 ID + 页码 + 字符偏移 → 前端可跳转到原文对应位置。

---

##### 节点 12：`refuse` — 拒答

**结构图**

```
assess 判定不足
      ↓
固定话术："知识库中未找到相关依据"
+ 可附带提示（如"该类问题建议咨询教务处"）
      ↓
refused = true, refusal_reason = "insufficient_evidence"
      ↓
记入 qa_logs
      ↓
     END
```

**拒答数据的二次价值**：`qa_logs` 中 `is_refused = true` 的问题清单，等于「学生问了但知识库答不上来」的清单 —— 管理端仪表盘展示后，可反哺知识库补充文档。答辩时这是一个完整的闭环叙事。

---

### 3.6 检索组件

| 组件 | 文件 | 职责 |
|---|---|---|
| BM25 | `retrieval/bm25.py` | jieba 分词 + BM25S 索引管理（构建/增量/持久化/失效） |
| Vector | `retrieval/vector.py` | Chroma 封装，含动态过采样逻辑 |
| Fusion | `retrieval/fusion.py` | RRF，k 值可配置，支持加权（消融实验用） |
| Reranker | `retrieval/reranker.py` | Cross-Encoder，含降级链 |
| Filters | `retrieval/filters.py` | 把 UserContext + 日期拼装成过滤条件（**唯一入口**） |

**`filters.py` 是唯一入口**：过滤条件只在这里生成，向量检索和 BM25 都调它。避免"两条路过滤规则不一致"导致越权——这是安全相关的强约束。

---

### 3.7 接口层

#### 3.7.1 认证

```
POST /api/auth/login     → { access_token, refresh_token, user }
POST /api/auth/logout
GET  /api/auth/me
```

#### 3.7.2 User 端

```
POST /api/chat/stream                   SSE
GET  /api/conversations
POST /api/conversations
GET  /api/conversations/{id}/messages
PATCH /api/conversations/{id}
DELETE /api/conversations/{id}
```

**SSE 事件协议**

| 事件 | 载荷 | 说明 |
|---|---|---|
| `session_created` | `session_id` | 新会话 |
| `resolved` | `resolved_query` | 消解补全结果，前端可展示 |
| `route` | `route` | 路由类别 |
| `token` | `text` | 逐字回答 |
| `citations` | `Citation[]` | 引用列表 |
| `refused` | `reason` | 触发拒答 |
| `done` | `latency_ms` | 结束 |
| `error` | `code, message` | 异常 |

#### 3.7.3 管理端

```
文档：
  POST   /api/admin/documents/upload
  GET    /api/admin/documents/upload/{task_id}/stream   SSE 进度
  GET    /api/admin/documents
  GET    /api/admin/documents/{group_id}/versions
  PATCH  /api/admin/documents/{id}
  POST   /api/admin/documents/{id}/disable
  POST   /api/admin/documents/{id}/enable
  DELETE /api/admin/documents/{id}

仪表盘：
  GET /api/admin/stats/overview
  GET /api/admin/stats/trend?days=30
  GET /api/admin/stats/retrieval
  GET /api/admin/stats/refusals
  GET /api/admin/stats/hot-questions

评测：
  POST /api/admin/eval/run
  GET  /api/admin/eval/runs
  GET  /api/admin/eval/runs/{id}
```

---

## 四、前端设计

### 4.1 工程结构

```
frontend/web/
├── src/
│   ├── api/            请求封装、SSE 封装、拦截器（两端共用）
│   ├── stores/         Pinia: auth / chat / conversation / admin
│   ├── router/         路由定义 + 角色守卫
│   ├── layouts/
│   │   ├── UserLayout.vue    聊天为主
│   │   └── AdminLayout.vue   侧边导航
│   ├── views/
│   │   ├── login/
│   │   ├── chat/             User 端
│   │   └── admin/            管理端
│   ├── components/     共用组件
│   └── utils/
└── vite.config.ts
```

**技术栈**：Vue 3 + Vite + Vue Router + Pinia + Element Plus + ECharts + markdown-it

**单工程双端的实现**：路由按 `/chat/*` 与 `/admin/*` 分组，全局前置守卫校验 `role`，非 admin 访问 `/admin/*` 直接重定向。共用组件（消息气泡、Markdown 渲染、SSE 封装、请求拦截器）只写一份。

### 4.2 User 端

```
┌──────────────┬────────────────────────────────┐
│              │                                │
│  会话列表     │        消息区                   │
│  · 新建       │   ┌────────────────────────┐  │
│  · 置顶       │   │ 用户消息                │  │
│  · 重命名     │   └────────────────────────┘  │
│  · 删除       │   ┌────────────────────────┐  │
│  · 分页       │   │ "我理解你在问：…"       │  │  ← resolved 事件
│              │   │ 助手回答（流式）         │  │
│              │   │ ┌──────────────────┐   │  │
│              │   │ │ 📚 引用            │   │  │  ← citations 事件
│              │   │ │ · 文档名 · 章节    │   │  │
│              │   │ │ · 片段 · [查看原文]│   │  │  ← 点击回跳
│              │   │ └──────────────────┘   │  │
│              │   └────────────────────────┘  │
│              ├────────────────────────────────┤
│              │ 输入框                    [发送]│
└──────────────┴────────────────────────────────┘
```

**关键交互**

| 交互 | 实现 |
|---|---|
| 流式渲染 | SSE `token` 事件逐字追加，Markdown 增量渲染 |
| 消解提示 | 收到 `resolved` 且与原文不同时，弱化展示"我理解你在问…" |
| 引用回跳 | 点击引用 → 跳转原文视图，定位到页码 + 字符偏移 |
| 拒答展示 | 收到 `refused` → 差异化样式 + 提示补充咨询渠道 |
| 断线重连 | SSE 断开自动重连 + 轮询兜底 |
| 澄清交互 | 收到 `route=clarify` → 反问作为普通助手消息展示 |

### 4.3 管理端

```
┌──────────┬────────────────────────────────────────┐
│          │                                        │
│ 仪表盘    │  ┌──────────────────────────────────┐ │
│ 文档管理  │  │  文档列表                         │ │
│ 版本管理  │  │  标题│状态│可见│版本│生效日│操作   │ │
│ 拒答分析  │  │  ────────────────────────────────  │ │
│ 评测      │  │  ...                              │ │
│          │  └──────────────────────────────────┘ │
│          │  [+ 上传]  [批量上传 ZIP]               │
└──────────┴────────────────────────────────────────┘
```

| 页面 | 功能 |
|---|---|
| **文档管理** | 列表（筛选：状态/可见范围/关键词）、单文件与 ZIP 上传（SSE 进度条）、删除、启用/停用、编辑可见范围与生效日期 |
| **版本管理** | 按 `doc_group_id` 折叠展示，展开显示历次版本；当前生效版本高亮 |
| **拒答分析** | 拒答问题 Top N 列表 + 时间分布；标注"建议补充该文档" |
| **评测** | 触发评测、历史记录、消融实验对比表（**亮点②的展示窗口**） |

### 4.4 仪表盘

| 图表 | 数据源 | 类型 |
|---|---|---|
| 核心指标卡 | `stats/overview` | 数字卡：文档数 / chunk 数 / 问答量 / 拒答率 |
| 问答量趋势 | `stats/trend` | 折线图（近 30 天） |
| 检索性能 | `stats/retrieval` | 节点耗时分布柱状图 + 降级次数 |
| 拒答分布 | `stats/refusals` | 列表 + 饼图 |
| 高频问题 | `stats/hot-questions` | 横向柱状图 |

---

## 五、评测方案（亮点②）

### 5.1 测试集

| 类型 | 占比 | 题量 | 考察 |
|---|---|---|---|
| 事实型（单文档可答） | 40% | 60–80 | 基础能力 |
| 多跳型（跨文档推理） | 20% | 30–40 | 融合与精排 |
| **文号 / 专有名词型** | 20% | 30–40 | **直接验证 BM25 的价值** |
| 拒答型（库中确实没有） | 10% | 15–20 | 幻觉抑制 |
| 多轮指代型 | 10% | 15–20 | 消解模块 |
| **合计** | 100% | **150–200** | |

每条包含：`question` / `ground_truth` / 相关文档与 chunk 标注 / 类型标签。

### 5.2 指标

ragas 四指标：`Faithfulness`（忠实度）、`Answer Relevancy`（答案相关性）、`Context Precision`、`Context Recall`。

### 5.3 消融实验

**主表** —— 逐项叠加，量化每一步的增益：

| 配置 | Context Recall | Context Precision | Faithfulness | 平均耗时 |
|---|---|---|---|---|
| 纯向量检索 | | | | |
| + BM25 中文分词修复 | | | | |
| + RRF 融合 | | | | |
| + Cross-Encoder 精排 | | | | |
| + HyDE 改写 | | | | |
| + ACL / 版本过滤 | | | | |
| **完整链路** | | | | |

**分层表** —— 按问题类型拆解，证明"混合检索对文号型问题的增益"：

| 问题类型 | 纯向量 Recall | 完整链路 Recall | 增益 |
|---|---|---|---|
| 事实型 | | | |
| 多跳型 | | | |
| 文号 / 专有名词型 | | | |
| 多轮指代型 | | | |

**这张分层表是回应模块②立项理由的直接证据。**

### 5.4 回归

评测固化为可重复执行的流程，每次重大改动后重跑，指标不得低于基线。

---

## 六、测试策略

现有项目**零测试**，这是"上线级质量"最明显的短板。

| 层次 | 覆盖对象 | 优先级 |
|---|---|---|
| 单元 | 分块逻辑、RRF 融合、过滤条件拼装、版本状态机 | 高 |
| 集成 | 摄入→检索→生成全链路 | 高 |
| **安全** | **ACL 隔离：证明 A 角色检索不到受限文档** | **高（必须有）** |
| 契约 | 接口返回结构，保障前后端并行 | 中 |
| 评测回归 | ragas 指标不低于基线 | 中 |

**安全测试是硬要求**：需有明确用例，如「student 角色提问，断言返回结果中不含 `visible_roles=["admin"]` 的文档 chunk」。

---

## 七、实施里程碑

每个里程碑结束系统均可运行。

| # | 里程碑 | 交付内容 | 人天 | 验收标准 |
|---|---|---|---|---|
| **M0** | 骨架闭环 | 新工程结构、FastAPI 骨架、LangGraph 图骨架（节点先填简实现）、SQLite + Chroma 接通、最简 Vue 聊天页 | 8–12 | **端到端跑通**：传文档 → 提问 → 流式作答 + 引用 |
| **M1** | 检索做对 | jieba 修复 + BM25S 迁移 + 索引持久化、RRF、精排降级、ACL + 版本过滤 + 动态过采样 | 12–16 | 检索指标有基线数据 |
| **M2** | 查询理解 | resolve 节点（含条件跳过）、三分类路由、clarify 分支、HyDE 重写、机制化拒答 | 10–14 | 多轮指代场景通过 |
| **M3** | 生成与引用 | 上下文组装、答案生成、引用统一、页码/章节/偏移定位、原文回跳 | 10–14 | 点击引用可跳转原文 |
| **M4** | Vue 两端 | User 端完整、管理端、仪表盘 | 18–24 | 全功能可用 |
| **M5** | 评测与打磨 | 测试集构建、ragas 接入、消融实验、测试补齐、可观测性、部署配置 | 12–18 | 消融实验表产出 + 测试通过 |
| | **合计** | | **70–98** | 含返工余量 **80–120 人天** |

**顺序理由**：M1 排在检索优先，因为检索质量是整个系统的天花板——检索不对，后续提示词优化无从发挥。且 M1 结束即可产出第一批可量化数据。

**M0 是风险控制点**：全量重写下"什么都跑不起来"的窗口压到 8–12 天。

---

## 附：现有项目需同步处理的问题

重构时一并解决，避免带入新项目：

| # | 问题 | 位置 | 处理 |
|---|---|---|---|
| 1 | MinerU 密钥硬编码且已提交 git | `app/config/chroma.yaml` | 吊销重发 + 改环境变量 + 清理 git 历史 |
| 2 | `user_id` 为客户端传入参数，无校验 | 全部接口 | 一律从 JWT 取，接口不再接受该参数 |
| 3 | intent 分类器配置项在两个 YAML 中均不存在，静默使用代码默认值 | `intent_classifier.py` | 新项目配置项与代码严格对应 |
| 4 | `mode="auto"` 声明但未实现，静默降级为 agent | `chat_service.py` | 新项目无此参数（模式由架构决定） |
| 5 | 知识库接口存在两套命名空间 | `/knowledge` 与 `/api/knowledge` | 统一为 `/api/admin/documents` |
| 6 | 引用机制两套并存（代码计算 vs LLM 自写） | `chat_service.py` / `agent.txt` | 统一为代码计算，见 3.5.3 节点 11 |
| 7 | 上下文组装逻辑在两个文件中重复 | `rag_service.py` / `agent_service.py` | 收敛为单一 `build_context` 节点 |
| 8 | 零测试、无 CI | 全项目 | 见第六章 |

---

**待审批**。确认后进入实施计划编写。
