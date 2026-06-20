# 本地知识库检索系统  

>| 向量数据库：ChromaDB 1.5+ | LLM 框架：LangChain 1.3+ | Web 框架：FastAPI | 更新时间：2026-06-20

---

## 目录

1. [项目概述](#一项目概述)
2. [技术栈总览](#二技术栈总览)
3. [系统架构](#三系统架构)
   - [3.0 rag系统文件上传流程](#30-rag系统文件上传流程)
   - [3.0.1 rag系统检索流程](#301-rag系统检索流程)
   - [3.1 分层架构](#31-分层架构)
   - [3.2 数据结构流转](#32-数据结构流转)
   - [3.3 项目目录结构](#33-项目目录结构)
4. [后台初始化机制](#四后台初始化机制)
5. [文档上传与处理流程](#五文档上传与处理流程)
   - [5.1 上传入口](#51-上传入口)
   - [5.2 文件验证](#52-文件验证)
   - [5.3 文档加载策略](#53-文档加载策略)
   - [5.4 PDF 多模态解析](#54-pdf-多模态解析)
   - [5.5 文本切分策略](#55-文本切分策略)
   - [5.6 向量库写入策略](#56-向量库写入策略)
   - [5.7 MD5 去重机制](#57-md5-去重机制)
6. [检索策略](#六检索策略)
   - [6.1 查询改写与必要性分类器](#61-查询改写与必要性分类器)
   - [6.2 混合检索架构](#62-混合检索架构)
   - [6.3 重排序](#63-重排序)
   - [6.4 Agent 智能体编排](#64-agent-智能体编排)
7. [删除与清理](#七删除与清理)
8. [容错与自我修复](#八容错与自我修复)
   - [8.1 全链路日志与实时监控](#81-全链路日志与实时监控)
9. [配置系统](#九配置系统)
10. [其他改进建议](#十其他改进建议)
11. [核心文件索引](#十一核心文件索引)
12. [会话记忆设计](#十二会话记忆设计)

---

## 一、项目概述

本项目是一个基于 **FastAPI + LangChain + ChromaDB** 构建的 RAG（检索增强生成）知识库服务，并集成 LangChain Agent 智能体能力。支持用户上传多种格式文档，自动完成文档解析、文本切分、向量化存储，并提供混合检索（向量 + BM25）和重排序能力。Agent 层通过 LangChain 工具链编排，实现检索增强推理与多步决策。

### 核心能力

| 能力 | 说明 |
|------|------|
| 文档解析 | 支持 PDF、TXT、Markdown、PPTX、DOCX 五种格式 |
| PDF 多模态解析 | 提取 PDF 中的图片，通过视觉大模型生成图片描述，补充纯文本提取的盲区 |
| 文本切分 | RecursiveCharacterTextSplitter，支持中文语义分隔符 + 可选语义合并 |
| 向量存储 | ChromaDB 持久化，按用户隔离（当前默认使用同一 `user_id`） |
| 混合检索 | 向量检索 + BM25 关键词检索，原查询走 BM25，改写查询走向量，RRF 排名融合 |
| 重排序 | BGE-Reranker-v2-m3 CrossEncoder 模型 |
| Agent 智能体 | LangChain Agent 工具链编排，检索增强推理与多步决策 |
| 去重 | 文件级 MD5 去重 |
| 会话记忆 | RunnableWithMessageHistory + SQLite 持久化，多轮对话上下文保持 |
| 自动降级 | ChatModel `with_fallbacks()` 机制，DeepSeek 失败自动切换 ChatTongyi |
| 配置管理 | `.env` + `chroma.yaml` 集中管理 50+ 配置项，零硬编码 |

---

## 二、技术栈总览

### LLM 与大模型

| 组件 | 技术选型 | 备选方案 | 说明 |
|------|----------|----------|------|
| Chat 模型 | DeepSeek 官网 (deepseek-chat) | ChatTongyi (qwen3-max) | DeepSeek 优先，`with_fallbacks()` 自动降级到 ChatTongyi |
| Embedding 模型 | DashScope (text-embedding-v4) | OllamaEmbeddings (qwen3-embedding:0.6b) | 通过 LangChain Embedding 工厂环境变量切换 |
| 视觉模型 | ChatTongyi (qwen3.7-max-2026-06-08) | ChatOllama (qwen-vl:7b) | 通过 VISION_MODEL_TYPE 环境变量切换 |
| 重排序模型 | BGE-Reranker-v2-m3 (CrossEncoder) | — | 从 ModelScope 下载，本地运行 |

### Web 框架与基础设施

| 组件 | 技术选型 | 用途 |
|------|----------|------|
| Web 框架 | FastAPI | REST API |
| 异步处理 | asyncio | 异步文件 I/O 与 LLM 调用 |
| LLM 框架 | LangChain 1.3+ / LangChain-Core 1.4+ / LangChain-Community | 文档加载、切分、检索链、Agent 编排 |
| Agent 框架 | LangChain-Classic 1.0+ | create_tool_calling_agent + AgentExecutor |
| 向量数据库 | ChromaDB 1.5+ via langchain-chroma 1.1+ | 向量持久化存储，通过 `langchain_chroma.Chroma` 统一管理 |
| 会话存储 | SQLite | 对话记录全量持久化 |
| 文件类型检测 | python-magic | MIME 类型验证 |

### 文档处理关键库

```
langchain 1.3, langchain-core 1.4, langchain-community 0.4, langchain-classic 1.0
langchain-chroma 1.1 (向量存储), langchain-openai 1.3 (DeepSeek 兼容接口)
pymupdf (fitz) — PDF 文本/图片提取 + 页面渲染
sentence-transformers 5.6 — CrossEncoder 重排序 (bge-reranker-v2-m3)
dashscope 1.25 — 阿里云百炼 SDK（text-embedding-v4 / qwen3.7-max）
modelscope 1.37 — 模型下载
aiofiles 25.1 — 异步文件 I/O
imagehash 4.3 — 感知哈希去重
sqlite3 — 会话记忆持久化
```

---

## 三、系统架构

### 3.0 rag系统文件上传流程

```
文件上传
    │
    ▼
大小校验 ≤30MB
    │
    ▼
MIME类型双重校验（MIME类型 + 扩展名，二者之一匹配即可）
    .pdf / .txt / .md / .pptx / .docx
    │
    ├── <10MB (小文件)
    │   │
    │   ▼
    │   读入内存 (BytesIO)，不落磁盘
    │   │
    │   ▼
    │   MD5 计算 (内存)
    │
    └── 10-30MB (大文件)
        │
        ▼
        流式上传接收，边读边写入 .tmp 临时磁盘文件
        │
        ▼
        MD5 计算 (磁盘)
    │
    ▼
┌────────────────────────────────────────────────┐
│ 阶段1：同步快速接收（秒级响应）                    │
│  原子rename缓存文件: {task_id}_{md5}.pdf         │
│  生成task_id，写入任务状态: accepted              │
│  返回接口 202 {task_id, status:"accepted"}       │
└────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────┐
│ 阶段2：后台异步队列消费解析                        │
│  小文件：从内存 BytesIO 读取，无需磁盘IO           │
│  大文件：读取缓存PDF执行解析链路                   │
└────────────────────────────────────────────────┘
    │
    ▼
{MD5 是否已存在?}
    ├── 是 → 跳过解析，复用已有向量 → 更新任务状态 done
    └── 否
        │
        ▼
    非PDF格式 → 各格式Loader加载
        │
        ├── 成功 → 跳至「文本清洗」
        │
        └── 失败 → 降级尝试
            │   ├── .md: UnstructuredMarkdownLoader → TextLoader 兜底
            │   ├── .docx: Docx2txtLoader → python-docx 兜底
            │   └── .pptx: UnstructuredPowerPointLoader → python-pptx 兜底
            │
            ├── 降级成功 → 跳至「文本清洗」
            │
            └── 降级也失败 → 诊断兜底（5.3.1）
                │   ├── 空文件检测
                │   ├── 文件头魔数检测
                │   └── 统一输出: 前端错误提示 + 日志记录
                │
                └── 终止处理，提示用户重传
        │
        ▼
    PDF格式
        │
        ▼
    PDF前置校验
        ├── 加密PDF → 尝试空密码解密 → 成功继续 / 失败提示用户输入密码
        ├── 文件损坏 → 直接终止，提示重传
        └── 正常
            │
            ▼
        提取内嵌图片 extract_images_from_pdf
            存储路径: data/extracted_images/{user_id}/{md5}/p{page}_i{idx}.{ext}
            │
            ▼
        PDF图层类型判定
            页面存在图片 + 文本字符 < 100 → 标记需视觉处理
            │
            ├── 纯文本PDF (text_pdf): 无图片、每页文本≥100字符
            │   │
            │   ▼
            │   PyMuPDF 提取全文/标题/表格
            │   │   ├── 成功 → 纯文本Block
            │   │   └── 失败 → pdfplumber 兜底 → 仍失败 → 文件损坏，提示重传
            │
            ├── 图文混合PDF (mix): 同时存在TEXT和IMAGE对象
            │   │
            │   ▼
            │   pdfplumber 提取正文、表格、图表bbox坐标
            │   │
            │   ▼
            │   PyMuPDF 根据bbox裁切图表局部区域
            │   │
            │   ▼
            │   页面感知哈希去重 (pHash, 汉明距离≤10)
            │   │   └── 同组仅代表页调用多模态，其余复用
            │   │
            │   ▼
            │   批量并发多模态调用 (BATCH_SIZE=5, asyncio.gather)
            │   │   ├── 主: 阿里云百炼多模态LLM
            │   │   └── 降级: 本地Ollama多模态 → 最终兜底: 仅保留原生文本
            │   │
            │   ▼
            │   按y坐标拼接: 前文 + 图表描述 + 后文
            │
            └── 扫描PDF (scan_pdf): 无TEXT对象，仅图片
                │
                ▼
                PyMuPDF 渲染 144dpi 整页图片
                │
                ▼
                OpenCV 预处理: 灰度 → 二值化 → 倾斜矫正 → 裁白边 → 降噪
                │
                ▼
                页面哈希去重 → 重复页复用代表页结果
                │
                ▼
                整页图片送入多模态统一识别
                │   ├── 主: 云端多模态
                │   └── 降级: 本地Ollama → 最终兜底: [本页扫描图像识别失败]
            │
            ▼
        统一结构化封装
            block_type: text/table/chart/image
            文本融合: 原生文本 + [页面视觉描述] 拼接
            │
            ▼
        进入「文本清洗」
    │
    ▼
文本清洗（全格式统一，切分前执行）
    ├── ① 控制字符清理: 零宽字符、换页符、垂直制表符、空字符
    ├── ② 空白规范化: 连续空行压缩、行首尾trim、连续空格压缩
    ├── ③ 页眉页脚清除: ≥3页相同行 → 标记为页眉/页脚并移除
    ├── ④ 模型标记清理: 移除 "--- Page N ---" 分隔符等残留
    └── ⑤ 空内容过滤: page_content 为空 → 丢弃
    │
    ▼
{内容是否为空?}
    ├── 是 → 跳过
    └── 否
        │
        ▼
    RecursiveCharacterTextSplitter 切片
        chunk_size=500, chunk_overlap=50
        │
        ▼
    {切片是否为空?}
        ├── 是 → 跳过
        └── 否
            │
            ▼
        注入元数据 user_id + original_filename + md5 + page_num + image_paths
            │
            ▼
        ChromaDB.add_documents() → Embedding向量化 → SQLite持久化
            │
            ▼
        保存MD5记录
            │
            ▼
        更新任务状态: done
            │
            ▼
        清理临时文件
            ├── 小文件: 释放内存
            └── 大文件: 删除 .tmp 磁盘文件
```

### 3.0.0 压缩包上传流程

```
压缩包上传 (.zip / .tar.gz / .rar)
    │
    ▼
┌─ Zip压缩包上传（独有外层逻辑）──────────────┐
│ 文件校验                                   │
│   ├── 扩展名白名单 → 不通过 → 返回 400      │
│   └── 大小 ≤ 300MB → 不通过 → 返回 413      │
│                                             │
│ 创建 zip_batch_task_id，状态 pending        │
│   │  POST /api/knowledge/upload_zip         │
│   │  → {"task_id": "zip_abc123",            │
│   │     "status": "pending"}                │
│                                             │
│ 后台异步处理 (zip_handler.py)               │
│   ├── 1. 解压到隔离临时目录                 │
│   │      data/tmp/zip_{task_id}/            │
│   │                                         │
│   ├── 2. 递归扫描过滤有效子文件              │
│   │   ├── 在 allow_knowledge_file_types → 加入队列 │
│   │   ├── 格式不支持 → 跳过(unsupported_format)    │
│   │   └── 解压后总大小 ≤ 200MB → 超限直接失败      │
│   │                                         │
│   ├── 3. 线程池并发 ─→ 逐个进入             │
│   │   │           【全局公共复用文档管道】    │
│   │   │              ↓                      │
│   │   │   ┌─────────────────────────────┐  │
│   │   │   │ ① MD5 全局查重               │  │
│   │   │   │ ② 文件格式分流               │  │
│   │   │   │ ③ 统一文本清洗               │  │
│   │   │   │ ④ 切片处理                   │  │
│   │   │   │ ⑤ 向量入库(全局并发写信号量)  │  │
│   │   │   │ ⑥ MD5 入库记录               │  │
│   │   │   └─────────────────────────────┘  │
│   │   │   返回 FileProcessResult            │
│   │   └── 单文件失败不中断整包              │
│   │                                         │
│   ├── 4. 聚合结果                           │
│   │   ├── 成功文件数 → success++            │
│   │   ├── 重复文件 → skipped (duplicate)    │
│   │   └── 失败文件 → failed + error_details │
│   │                                         │
│   ├── 5. 清理隔离临时目录                   │
│   │   └── 删除 data/tmp/zip_{task_id}/      │
│   │                                         │
│   ├── 6. 任一成功则刷新 BM25 缓存           │
│   │   └── HybridRetriever.invalidate_cache  │
│   │                                         │
│   └── 7. 更新任务为 completed               │
│       ├── progress: {total, success,        │
│       │             skipped, failed}         │
│       └── error_details: [{file_path,       │
│            error_type, reason}, ...]         │
│                                             │
└─────────────────────────────────────────────┘
    │
    ▼
前端轮询 GET /api/knowledge/task/{task_id}
    │
    ├── 成功文件 → 已入库，可检索
    ├── 跳过文件 → 提示「格式不支持，可单独上传」
    └── 失败文件 → 提示「以下文件解析失败，请单独重新上传」
```

### 3.0.1 rag系统文档检索流程

> **调用入口**：该流程由 Agent 的 `knowledge_search` 工具触发，Agent 通过 `POST /chat` 统一入口接收请求后，按需调用此工具。详见 [6.5 统一检索入口](#65-统一检索入口--chatpy)。

```
原始 Query
    │
    ▼
会话记忆加载
    ConversationMemoryService.load_context(session_id)
    加载最近 N 轮历史对话（默认 10 轮）
    │
    ▼
预处理：去空格
    │
    ▼
第一层：极简关键词判定
    ├── 条件①：不含 Q_WORDS（什么、怎么、如何…）和 PRO_WORDS（它、这个、那个…）
    └── 条件②：清洗后长度 ≤ 6
    │
    ├── 分支1：同时满足①② → 纯关键词（如 "RAG"、"LLM"、"GDP增长率"）
    │   │  need_rewrite = False
    │   └── 仅 BM25 检索 → 跳过向量检索
    │
    └── 分支2：不满足 → 进入第二层
        │
        ▼
第二层：简化改写三规则
    ├── 规则A：含 PRO_WORDS（它、这个、那个、前面…）
    ├── 规则B：含 Q_WORDS（什么、怎么、如何、？…）
    └── 规则C：有历史上下文 且 len < 15（兜底）
    │
    ├── 满足 A/B/C 任意一条 → need_rewrite = True
    │   │  LLM 改写 Query（HyDE 生成假设性文档）
    │   │
    │   └── 改写结果 → BM25 + 向量混合检索
    │
    └── A/B/C 全不满足 → need_rewrite = False
        │
        └── 原始 Query → BM25 + 向量混合检索
    │
    ▼
混合检索：BM25 + 向量
    │
    ├──────────────────────────────────────────┐
    │  asyncio.gather 并行执行                   │
    │                                           │
    ▼                                           ▼
向量检索：ChromaDB similarity_search        BM25 检索：关键词匹配
    filter={'user_id': user_id}                 从缓存读取 BM25 索引（LRU）
    输入：改写结果 或 原查询                      缓存失效 → 从 ChromaDB 重建
    │                                           │
    └──────────────────────────────────────────┘
                    │
                    ▼
            RRF 排名融合 (k=60)
            score(doc) = Σ 1/(k + rank_i)
                    │
                    ▼
合并去重 → 候选文档列表 (top_k × 2, 去重后 ≈ 6-10 条)
    │
    ▼
CrossEncoder 重排序 (BGE-Reranker-v2-m3)
    对每条 (query, doc) 打分 → 按分数降序排列
    │
    ▼
Top-N 结果 (默认 3 条)
    │
    ├── 普通检索模式
    │   │
    │   ▼
    │   拼接上下文 → LLM 生成回答 → 返回用户
    │
    └── Agent 智能体模式
        │
        ▼
        Agent 入口，注册工具链
        ├── 知识检索工具 (RAGService：HyDE + 检索 + 重排序 + 摘要)
        ├── 联网搜索工具 (WebSearch)
        └── 文档摘要工具 (Summarizer)
        │
        ▼
        ReAct / Tool Calling 推理循环
        ├── Thought: 分析意图，选择工具
        ├── Action: 调用工具获取结果
        └── Observation: 评估结果，判断是否继续
        │
        ▼
        多步推理结果合成 → 流式输出最终回答
```

### 3.1 分层架构

```
┌──────────────────────────────────────────────────────────┐
│                      API 路由层                           │
│  chat_router.py (+ knowledge_router + conversation_router)│
│  POST /chat                         — 统一对话入口（Agent）│
│  POST /knowledge/add/single                               │
│  POST /api/knowledge/upload_zip     — 压缩包上传（异步）   │
│  GET /api/knowledge/task/{task_id}  — 任务状态查询（轮询）  │
│  GET/DELETE /knowledge/md5/...                            │
│  GET /knowledge/documents/...                             │
│  GET /knowledge/image/...                                 │
│  POST /conversation/new                                   │
│  GET /conversations                                       │
│  GET /conversation/{id}/messages                          │
│  DELETE /conversation/{id}                                │
├──────────────────────────────────────────────────────────┤
│                     业务逻辑层                            │
│  chat_service.py (+ knowledge_service + conversation_svc) │
│  Agent 编排 → 工具调用 → 结果合成 → 流式输出               │
│  文件验证 → 加载 → 清洗 → 切分 → 存储                     │
│  zip_handler.py (压缩包解压 → 并行解析 → 错误收集)         │
│  会话管理 → 记忆加载 → 对话追加 → 生命周期                │
├──────────────────────────────────────────────────────────┤
│                     数据模型层                            │
│  schemas/models.py (Pydantic 请求/响应模型)               │
├──────────────────────────────────────────────────────────┤
│                     文档处理核心                          │
│  processor.py                                             │
│  加载 → 切分 → 元数据注入 → 存储                          │
│  text_spliter.py (RecursiveCharacterTextSplitter)         │
│  pdf_multimodal_loader.py (多模态PDF)                     │
│  file_handler.py (TXT/MD/PPTX/DOCX)                       │
├──────────────────────────────────────────────────────────┤
│                     向量存储层                            │
│  vector_store.py (ChromaDB 单例)                          │
│  md5_store.py (MD5去重存储)                               │
│  image_extractor.py (PDF图片提取)                         │
├──────────────────────────────────────────────────────────┤
│                     检索层                                │
│  hybrid_retriever.py (BM25 + 向量混合检索)                │
│  reorder_service.py (CrossEncoder 重排序)                 │
│  rag_service.py (RAG 核心：HyDE + 检索 + 重排序 + 摘要)   │
│  empty_retriever.py (空检索器，未登录时用)                │
├──────────────────────────────────────────────────────────┤
│                     Agent 层                              │
│  agent_service.py (LangChain Agent 编排)                  │
│  工具链：知识检索 / 联网搜索 / 文档摘要 / 多步推理       │
├──────────────────────────────────────────────────────────┤
│                     会话记忆层                            │
│  memory_service.py (LangChain Memory + SQLite 持久化)     │
├──────────────────────────────────────────────────────────┤
│                     基建层                                │
│  factory.py (模型工厂：Chat/Embed/Vision)                 │
│  background_init.py (后台异步初始化)                      │
│  vision_service.py (视觉模型调用服务)                     │
│  logger_handler.py (日志配置)                              │
│  success_response.py (统一成功响应)                        │
│  failed_response.py (统一异常处理)                         │
│  chroma.yaml / prompt.yaml                                │
└──────────────────────────────────────────────────────────┘
```

### 3.2 数据结构流转

```
用户上传文件
    │
    ├── <10MB → 读入内存 BytesIO，不落磁盘
    └── 10-30MB → 流式写入临时文件 (.tmp)
    │
    ▼
UploadFile (FastAPI)  →  BytesIO / .tmp  →  MD5 计算
    │
    ▼
Document 列表 (LangChain)  ←  各格式 Loader
    ├── page_content: "文本内容..."
    └── metadata: {page, source, image_paths, has_images}
    │
    ▼
文本清洗（控制字符清理 → 空白规范 → 页眉页脚清除 → 模型标记清理）
    │
    ▼
RecursiveCharacterTextSplitter 切分
    │
    ▼
Document 列表 (切分后)
    ├── page_content: "切分后的文本片段" (<=500字符)
    └── metadata: {user_id, md5, original_filename, page, image_paths, ...}
    │
    ▼
ChromaDB.add_documents()  →  Embedding 向量化  →  SQLite 持久化
```

### 3.3 项目目录结构

```
Knowledge_rag_system/
│
├── main.py                                 # FastAPI 应用入口，启动 uvicorn
├── pyproject.toml                          # 项目依赖与元数据配置
├── .env                                    # 环境变量配置（API Key 等）
│
├── app/
│   ├── __init__.py
│   │
│   ├── core/                               # 核心基础设施
│   │   ├── __init__.py
│   │   ├── background_init.py              # 后台异步初始化管理器
│   │   ├── logger_handler.py               # 日志 Handler/Formatter 配置
│   │   ├── success_response.py             # 统一成功响应格式
│   │   └── failed_response.py              # 统一异常处理 + AppException
│   │
│   ├── config/                             # 配置文件
│   │   ├── __init__.py
│   │   ├── loader.py                       # 统一配置加载器（chroma.yaml 缓存）
│   │   ├── chroma.yaml                     # 30+ 项：检索/切分/阈值/词表/MIME/魔数
│   │   ├── prompt.yaml                     # Prompt 模板路径映射
│   │   └── prompts/                        # 6 个 Prompt 模板
│   │       ├── system.txt                  # 系统级 Agent Prompt
│   │       ├── hyde.txt                    # HyDE 假设性文档生成
│   │       ├── agent.txt                   # Agent 推理 Prompt
│   │       ├── summary.txt                 # 文档摘要 Prompt
│   │       ├── rewrite.txt                 # 查询改写 Prompt
│   │       └── vision.txt                  # 视觉模型描述 Prompt
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── models.py                       # Pydantic 请求/响应模型
│   │
│   ├── router/                             # API 路由层（13 个端点）
│   │   ├── __init__.py
│   │   ├── chat_router.py                  # POST /chat 统一对话入口（SSE 流式）
│   │   ├── chat_service.py                 # 会话管理 + Agent 编排
│   │   ├── knowledge_router.py             # 5 个知识库端点
│   │   ├── knowledge_service.py            # 文件校验 + 三层联动删除
│   │   ├── conversation_router.py          # 5 个会话端点
│   │   ├── conversation_service.py         # 会话管理业务逻辑
│   │   └── zip_router.py                   # 压缩包上传 + 任务查询
│   │
│   ├── rag/                                # RAG 核心模块
│   │   ├── __init__.py
│   │   ├── vector_store.py                 # langchain_chroma.Chroma 单例管理
│   │   ├── text_spliter.py                 # 文本切分（RecursiveCharacterTextSplitter）
│   │   ├── reorder_service.py              # 重排序（CrossEncoder，配置驱动）
│   │   ├── rag_service.py                  # RAG 核心（HyDE+混合检索+重排序+LCEL摘要）
│   │   │
│   │   ├── document_handler/
│   │   │   ├── __init__.py
│   │   │   └── processor.py                # 文档处理全链路 + 诊断兜底
│   │   │
│   │   ├── retrievers/
│   │   │   ├── __init__.py
│   │   │   ├── hybrid_retriever.py         # BM25(LRU缓存)+向量并行+RRF融合
│   │   │   ├── query_rewriter.py           # 两层分类器+HyDE改写（配置驱动）
│   │   │   └── empty_retriever.py          # 空检索器占位
│   │   │
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   └── agent_service.py            # langchain_classic Agent 编排
│   │   │
│   │   ├── md5_manager/
│   │   │   ├── __init__.py
│   │   │   └── md5_store.py                # MD5 JSON Lines 去重存储
│   │   │
│   │   └── zip_handler/
│   │       ├── __init__.py
│   │       └── zip_handler.py              # 压缩包解压+并行解析+错误收集
│   │
│   ├── memory/
│   │   ├── __init__.py
│   │   └── memory_service.py               # SQLChatMessageHistory + SQLite 持久化
│   │
│   └── utils/
│       ├── __init__.py
│       ├── factory.py                      # 模型工厂（with_fallbacks 自动降级）
│       ├── file_handler.py                 # 多格式加载器（配置驱动编码/类型）
│       ├── pdf_multimodal_loader.py        # PDF 三分支多模态解析
│       ├── image_extractor.py              # PDF 图片提取
│       ├── vision_service.py               # 视觉服务（Prompt 模板化）
│       ├── prompt_loader.py                # Prompt 统一加载器
│       ├── path_tool.py                    # 路径统一管理
│       └── log_tool.py                     # 日志统一管理
│
├── data/                                   # 数据持久化目录
│   ├── chromadb/                           # ChromaDB 向量数据库文件
│   ├── md5_hex_store/                      # MD5 去重记录存储
│   │   └── md5_hex_store.txt
│   ├── extracted_images/                   # PDF 提取的图片缓存
│   │   └── {user_id}/
│   │       └── {md5}/
│   │           └── p{page}_i{idx}.{ext}
│   └── tmp/                                 # 压缩包解压临时目录（处理完成后自动清理）
│
├── db/                                     # 会话记忆数据库目录
│   └── conversation.db                     # SQLite 对话记录全量存储
│
├── logs/                                   # 日志文件目录
│   ├── agent_20260618.log                  # Agent 业务日志
│   ├── rag_20260618.log                    # RAG 检索日志
│   └── ...
│
└── models/                                 # 本地模型文件（可选）
    └── bge-reranker-v2-m3/                 # 重排序模型（ModelScope 下载）
```

> **模块依赖关系**：`router` → `rag` + `memory` → `utils` → `core` → `config`，上层依赖下层，下层不感知上层。

---

## 四、后台初始化机制

### 4.1 设计动机

**避免模块级导入阻塞 uvicorn 启动。** 大型模型（Chat、Embedding、CrossEncoder 重排序模型）加载可能需要几十秒，如果在模块导入阶段同步加载，会导致 FastAPI 服务长时间无响应，甚至触发健康检查超时。

### 4.2 初始化流程

采用 `_BackgroundInitManager` 单例，在 FastAPI `startup` 事件中通过 `asyncio.create_task` 启动后台初始化：

```
uvicorn 启动 → startup 事件 → init_manager.start()
    │
    └── asyncio.create_task(_initialize_all())
            │
            ├── 1. _init_models()          → ChatModel + EmbedModel + VisionModel
            │      └── models_ready.set()   ← 信号通知
            │
            ├── 2. _init_chromadb()          → ChromaDB (等待 models_ready)
            │      └── chromadb_ready.set()
            │
            └── 3. _init_reranker()         → CrossEncoder + 模型下载
                   └── reranker_ready.set()
```

### 4.3 依赖关系

```
ChatModel ────┐
EmbedModel ───┼── models_ready ──→ ChromaDB 向量数据库
VisionModel ──┘

Reranker 模型下载 ──→ ReorderService
```

### 4.4 延迟加载模式

所有重型资源采用延迟加载，在首次实际使用时才解析实例：

| 组件 | 包装类 | 解析时机 |
|------|--------|----------|
| Embedding 模型 | `_LazyEmbedding` | 首次 `embed_documents` / `embed_query` |
| 视觉模型 | `VisionService._get_model()` | 首次调用视觉描述 |
| 重排序模型 | `ReorderService._get_model()` | 首次调用重排序 |

---

## 五、文档上传与处理流程

### 5.1 上传入口

| 入口 | 路由 | 限流 | 特点 |
|------|------|------|------|
| 单文件上传 | POST /knowledge/add/single | 5次/分钟 | 小文件 (<10MB) 读入内存；大文件 (10-30MB) 流式写入磁盘临时文件 |

### 5.2 文件验证

**大小校验**：单文件 ≤30MB，最大不超过 30MB

**大小分流策略**：

| 文件大小 | 处理方式 | 说明 |
|----------|----------|------|
| < 10MB（小文件） | 读入内存 `BytesIO` | 不落磁盘，性能最优；内存占用可控 |
| 10-30MB（大文件） | 流式上传 → 写入 `.tmp` 临时磁盘文件 | 边接收边写入，避免内存爆炸；处理完成后立即清理临时文件 |

**MIME 类型双重校验**（MIME 类型 + 扩展名，二者之一匹配即可通过）：

| 扩展名 | MIME 类型 |
|--------|-----------|
| .pdf | application/pdf |
| .txt | text/plain |
| .md | text/markdown |
| .pptx | application/vnd.ms-powerpoint |
| .docx | application/vnd.openxmlformats-officedocument.wordprocessingml.document |

双重校验的原因：`python-magic` 通过分析文件头魔数检测真实类型，防止攻击者将 `.exe` 改名为 `.pdf` 绕过纯扩展名校验；同时保留扩展名匹配作为容错。

### 5.3 文档加载策略

#### 各格式加载器对比

| 扩展名 | Loader | mode | 关键特性 | 存在问题 |
|--------|--------|------|----------|----------|
| .txt | TextLoader | 默认 | utf-8 → gbk 编码回退 | — |
| .md | UnstructuredMarkdownLoader | single | 整个文件合并 | — |
| .pptx | UnstructuredPowerPointLoader | single | 所有幻灯片合并 | — |
| .docx | Docx2txtLoader | 默认 | 提取段落文本，保留自然换行 | — |
| .pdf | 多模态 / 纯文本双路径 | — | 见 5.4 节 | — |

#### 选型理由

**TXT 编码双回退**：中文 Windows 系统导出的 `.txt` 文件默认编码是 GBK，非 UTF-8。双编码回退避免加载失败。

**Markdown/PPTX 的 mode="single"**：让后续统一的 `RecursiveCharacterTextSplitter` 控制切分，保证所有格式的 chunk 策略一致（chunk_size=500, overlap=50），而非依赖各 Loader 特定行为。

**DOCX 使用 Docx2txtLoader**：`TextLoader` 将 DOCX 的 ZIP 二进制字节流当作文本读取，无法正确提取内容。改为 `Docx2txtLoader` 基于 python-docx 正确提取段落文本，保留自然换行结构，无需额外预处理即可投入切分流水线。

#### 5.3.1 加载失败诊断兜底

当各格式 Loader 及其降级路径全部失败时，执行诊断兜底。**原则：即使能提取到部分可读内容，也全部丢弃，提示用户检查文件后重新上传，并将诊断信息写入日志。**

```
非 PDF 格式 Loader 及降级全部失败
    │
    ▼
诊断兜底流程
    │
    ├── 步骤1：空文件检测
    │   │  file_size == 0？
    │   │
    │   ├── 是 → 诊断: "文件为空"
    │   │       原因: empty_file
    │   │       建议: "文件内容为空，请检查后重新上传"
    │   │
    │   └── 否 → 继续
    │
    ├── 步骤2：文件头魔数检测
    │   │  读取文件头部 8 字节，匹配已知魔数
    │   │
    │   ├── 匹配到已知格式但无法解析
    │   │   │  如 PDF 头 (%PDF) 但 PyMuPDF 加载失败
    │   │   │  如 ZIP 头 (PK) 但 python-docx/pptx 解析失败
    │   │   └── 诊断: "文件已损坏"
    │   │       原因: corrupted + 具体格式名
    │   │       建议: "文件可能已损坏，请重新导出/保存后上传"
    │   │
    │   ├── 匹配到不支持的格式
    │   │   │  如 PNG (‰PNG)、JPEG (ÿØÿ)、GIF (GIF8)
    │   │   └── 诊断: "格式不支持"
    │   │       原因: unsupported_format + 具体格式名
    │   │       建议: "不支持该格式，支持的格式：pdf/txt/md/docx/pptx"
    │   │
    │   └── 未匹配任何已知魔数
    │       └── 诊断: "无法识别"
    │           原因: unknown_format
    │           建议: "无法识别文件类型，请确认文件格式正确后重新上传"
    │
    └── 步骤3：统一输出
        │
        ├── 返回前端: {status: "failed", reason: "xxx", suggestion: "xxx", filename: "xxx"}
        │
        └── 写入日志 (logger.error)
            ├── 文件名
            ├── 文件大小
            ├── 文件头魔数 (hex)
            ├── 各 Loader 失败原因 (逐条)
            ├── 诊断结果 (reason)
            └── 完整堆栈 (DEBUG 模式)
```

##### 魔数映射表

| 文件头魔数 (hex) | 对应格式 | 诊断结果 |
|------|------|------|
| `%PDF` | PDF 文件 | 文件已损坏（PDF 解析失败） |
| `PK\x03\x04` | ZIP 容器（docx/pptx） | 文件已损坏（内部结构异常） |
| `‰PNG` | PNG 图片 | 不支持该格式 |
| `ÿØÿà` / `ÿØÿÛ` | JPEG 图片 | 不支持该格式 |
| `GIF8` | GIF 图片 | 不支持该格式 |
| `\xd0\xcf\x11\xe0` | 旧版 Office（.doc/.ppt） | 不支持旧版格式，请转为 docx/pptx |
| 其他 | 未知 | 无法识别文件类型 |

##### 诊断输出示例

```
# 示例1：损坏的 PDF 文件
前端返回: {
  "status": "failed",
  "reason": "corrupted",
  "detail": "PDF 文件已损坏，无法解析",
  "suggestion": "文件可能已损坏，请重新导出/保存后上传",
  "filename": "报告.pdf"
}

日志输出:
【诊断兜底】文件: 报告.pdf | 大小: 1024KB | 魔数: 25504446 (%PDF)
  Loader 失败: UnstructuredPDFLoader → PDFSyntaxError
  Loader 失败: PyPDFLoader → PDFSyntaxError
  诊断: corrupted → PDF 文件已损坏
```

```
# 示例2：将 PNG 图片改名为 .pdf 上传
前端返回: {
  "status": "failed",
  "reason": "unsupported_format",
  "detail": "PNG 图片",
  "suggestion": "不支持该格式，支持的格式：pdf/txt/md/docx/pptx",
  "filename": "图表.pdf"
}

日志输出:
【诊断兜底】文件: 图表.pdf | 大小: 512KB | 魔数: 89504e47 (‰PNG)
  诊断: unsupported_format → PNG 图片，非支持格式
```

##### 代码实现

```python
import struct

# 魔数映射表
MAGIC_SIGNATURES = {
    b'%PDF': ('corrupted', 'PDF 文件已损坏，无法解析'),
    b'PK\x03\x04': ('corrupted', 'ZIP 容器文件已损坏（内部结构异常）'),
    b'\x89PNG': ('unsupported_format', 'PNG 图片'),
    b'\xff\xd8\xff': ('unsupported_format', 'JPEG 图片'),
    b'GIF8': ('unsupported_format', 'GIF 图片'),
    b'\xd0\xcf\x11\xe0': ('unsupported_format', '旧版 Office 格式（.doc/.ppt），请转换为 docx/pptx'),
}


def diagnose_failure(file_bytes: bytes, filename: str, loader_errors: list[str]) -> dict:
    """
    诊断兜底：所有 Loader 及降级路径均失败后调用。
    优先检测魔数，给出明确失败原因和用户操作建议。
    返回结构化错误信息，同时写入日志。
    """
    file_size = len(file_bytes)

    # 步骤1：空文件检测
    if file_size == 0:
        _log_diagnosis(filename, file_size, None, loader_errors, 'empty_file', '文件为空')
        return {
            'status': 'failed',
            'reason': 'empty_file',
            'detail': '文件为空',
            'suggestion': '文件内容为空，请检查后重新上传',
            'filename': filename,
        }

    # 步骤2：读取文件头魔数
    magic_bytes = file_bytes[:8]
    magic_hex = magic_bytes.hex()

    for signature, (reason, detail) in MAGIC_SIGNATURES.items():
        if magic_bytes.startswith(signature):
            if reason == 'corrupted':
                suggestion = '文件可能已损坏，请重新导出/保存后上传'
            else:
                suggestion = '不支持该格式，支持的格式：pdf/txt/md/docx/pptx'

            _log_diagnosis(filename, file_size, magic_hex, loader_errors, reason, detail)
            return {
                'status': 'failed',
                'reason': reason,
                'detail': detail,
                'suggestion': suggestion,
                'filename': filename,
            }

    # 步骤3：无法识别
    _log_diagnosis(filename, file_size, magic_hex, loader_errors, 'unknown_format', '无法识别文件类型')
    return {
        'status': 'failed',
        'reason': 'unknown_format',
        'detail': '无法识别文件类型',
        'suggestion': '无法识别文件类型，请确认文件格式正确后重新上传',
        'filename': filename,
    }


def _log_diagnosis(filename: str, file_size: int, magic_hex: str | None,
                   loader_errors: list[str], reason: str, detail: str):
    """将诊断结果写入日志，便于后续调试"""
    logger.error(
        f"【诊断兜底】文件: {filename} | 大小: {file_size}B"
        f"{' | 魔数: ' + magic_hex if magic_hex else ''}"
    )
    for err in loader_errors:
        logger.error(f"  Loader 失败: {err}")
    logger.error(f"  诊断: {reason} → {detail}")
```

##### 调用位置

在 [processor.py](file:///D:/Knowledge_rag_system/app/rag/document_handler/processor.py) 的 `get_file_document` 方法中，当各 Loader 均返回空列表时，调用 `diagnose_failure` 进行诊断，并将结果返回给上层：

```python
# processor.py 中 get_file_document 的伪代码
document = await self._try_all_loaders(file_path, md5_hex, user_id)
if not document:
    # 收集所有 Loader 的失败原因
    loader_errors = self._collect_loader_errors()
    # 读取原始文件字节用于诊断
    file_bytes = await self._read_file_bytes(file_path)
    # 诊断兜底
    diagnosis = diagnose_failure(file_bytes, filename, loader_errors)
    # 向上抛出诊断异常，由全局异常处理器统一返回给前端
    raise DocumentLoadException(diagnosis)
```

##### 设计考量

- **全部丢弃而非部分保留**：部分可读内容可能缺失关键信息，用户基于不完整数据的检索结果会产生错误判断。不如明确告知失败原因，让用户修复后重新上传，保证入库数据的完整性和可靠性；
- **日志记录完整链**：每条 Loader 失败原因逐条记录，加上文件魔数和大小，后续排查时无需复现即可定位问题；
- **不修改原始文件**：诊断过程仅读取文件头部字节，不修改、不修复文件内容；
- **与全局异常处理器对接**：诊断结果以 `DocumentLoadException` 形式抛出，由 [failed_response.py](file:///D:/Knowledge_rag_system/app/core/failed_response.py) 统一转为前端友好响应。

在所有格式文档加载完成后、进入 `RecursiveCharacterTextSplitter` 切分之前，统一执行文本清洗，消除脏数据对检索质量的干扰。

#### 清洗流水线

```
加载后的 Document 列表
    │
    ▼
① 控制字符清理
  移除零宽字符、换页符(\f)、垂直制表符(\v)、空字符(\0) 等不可见控制字符
    │
    ▼
② 空白规范化
  连续空白行压缩为单空行，行首行尾空白 trim，连续空格压缩为单个空格
    │
    ▼
③ 页眉页脚模式清除
  正则匹配常见页眉页脚模式（如 "第 X 页 / 共 Y 页"、页码数字、重复标题行）
  连续出现≥3页的相同行标记为页眉/页脚并移除
    │
    ▼
④ 视觉模型输出标记清理
  移除多模态解析残留的 "--- Page N ---" 分隔符、模型输出前缀/后缀
    │
    ▼
⑤ 空内容过滤
  清洗后 page_content 为空或仅含空白字符的 Document 直接丢弃
    │
    ▼
清洁后的 Document 列表 → 进入切分
```

#### 清洗规则详表

| 步骤 | 处理对象 | 方法 | 示例 |
|------|----------|------|------|
| 控制字符 | 不可见字符 | `re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)` | 移除 `\x00`、`\x0c` 等 |
| 空白规范 | 多余空白 | 连续 `\n` 压缩→双换行，连续空格→单空格 | `"a   b"` → `"a b"` |
| 页眉页脚 | 重复模式 | 按页检测，≥3页相同行→移除 | "第 1 页 / 共 10 页" |
| 模型标记 | 视觉输出残留 | 正则移除 `--- Page N ---` 及模型前缀 | `--- Page 1 ---` 分隔符 |
| 空文档 | 无效内容 | `len(text.strip()) == 0` → 丢弃 | 全空白页 |

#### 设计考量

- **放在切分之前而非之后**：切分前清洗可避免清洗逻辑处理跨 chunk 边界问题，且每个清洗后的 Document 更干净，切分器产生的 chunk 质量更高；
- **不修改原始元数据**：清洗仅修改 `page_content` 字段，保留所有 metadata 用于溯源；
- **可配置开关**：通过环境变量 `TEXT_CLEAN_ENABLED=true` 控制，默认开启，方便调试时关闭排查问题。

---

### 5.4 PDF 多模态解析 — 轻量化精简版

> 整体架构：**上传异步缓存 → 前置加密/文件校验 → PDF图层类型判定（融合视觉触发规则）→ 三条差异化解析分支**。统一简化能力：图片提取、页面单级感知哈希去重、单套多模态批量并发、云端/本地双后端降级。统一结构化封装 → 文本分块+向量入库 → RAG检索溯源。

**依赖库精简：** pdfplumber、PyMuPDF (fitz)、OpenCV、imagehash、Pillow、numpy、多模态大模型API(Ollama/阿里云百炼)、asyncio 任务队列

#### 工具角色说明（删减冗余工具）

| 工具 | 角色 |
|------|------|
| PyMuPDF (fitz) | 主解析引擎，图层判定、文字提取、图片裁切、页面渲染、提取内嵌原图 |
| pdfplumber | 文本备用解析，表格识别、按排版坐标还原文字顺序 |
| imagehash | 页面感知哈希计算，重复页面去重 |
| OpenCV | 扫描件基础图像预处理：灰度、二值化、倾斜矫正、裁白边、降噪 |
| 阿里云百炼多模态LLM | 优先图文、图表、扫描识别主服务 |
| Ollama本地多模态LLM | 云端不可用时本地兜底识别 |

> **全局简化原则：**
> 1. 移除独立OCR链路，全部图文识别交由多模态模型统一处理；
> 2. 只保留**页面级哈希去重**，删除图表单独哈希计算；
> 3. 移除pdfminer、视觉结果缓存、扫描件区块轮廓分割；
> 4. 简化重试、缓存、渲染分辨率逻辑，砍掉复杂分支；
> 5. 不引入 Ghostscript PDF修复工具，文件损坏直接提示重传。

---

#### 5.4.1 环境依赖安装

```bash
# 精简后Python依赖
pip install pdfplumber pymupdf opencv-python pillow numpy imagehash
```

---

#### 5.4.2 PDF 上传预处理、异步缓存、任务管理（大幅简化重试逻辑）

##### 上传分层处理流程

阈值 10MB 区分大小文件，保留异步核心能力，删除复杂指数退避、长期缓存：

```
用户上传PDF文件流
    │
    ▼
┌──────────────────────────────────────┐
│ 阶段1：同步快速接收（秒级响应）        │
│  文件大小 < 10MB：                    │
│    直接读入内存 BytesIO，不落磁盘     │
│  文件大小 10-30MB：                   │
│    流式上传，边接收边写入 .tmp 临时文件│
│  校验文件大小、Content-Length、MD5    │
│  校验通过原子rename缓存文件：          │
│    {task_id}_{md5}.pdf               │
│  生成task_id，写入任务状态：      │
│    accepted                          │
│  返回接口 202 {task_id, status:"accepted"}│
└──────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────┐
│ 阶段2：后台异步队列消费解析
│  小文件：从内存 BytesIO 读取，无需磁盘IO
│  大文件：读取缓存PDF执行解析链路
│  成功：清理临时缓存，状态改为done
│  失败：直接删除缓存，提示用户重新上传
└──────────────────────────────────────┘
```

##### 简化失败重试策略（核心简化点）

**取消差异化重试、免上传重解析、24h缓存TTL：**
1. 小文件（内存模式）解析失败 → 直接释放内存，提示重传；
2. 大文件（磁盘模式）解析失败 → 直接删除磁盘临时缓存，提示重传；
3. 不做多轮重试、无指数退避，减少定时清理、前端交互逻辑；
4. 瞬时网络/接口故障仅本地单次重试1次，彻底失败直接返回重传提示。

##### 任务状态查询接口

前端轮询 `/knowledge/task/{task_id}`，仅返回基础状态、进度；移除细分降级指标展示。

---

#### 5.4.3 PDF前置校验与增强图层判定（保留核心规则）

##### 加密 PDF 处理

1. PyMuPDF尝试密码解密打开
2. 解密成功 → 进入图层判定
3. 降级分支：文件损坏，直接终止解析，返回前端重传提示。无密码/密码错误 → 前端提示用户重新输入正确的 PDF 密码，或上传一份已解密的 PDF 文件

##### 增强图层判别（保留视觉触发核心规则）

仅两层文本解析引擎：PyMuPDF + pdfplumber，移除pdfminer.six。判定规则不变：页面存在图片 + 文本字符＜100 → 标记需视觉处理页面。

```python
import fitz
def judge_pdf_type(pdf_path, pdf_md5, user_id):
    doc = fitz.open(pdf_path)
    total_page = len(doc)
    pdf_type = "text_pdf"
    vision_need_page_nums = []
    page_image_map = extract_images_from_pdf(pdf_path, user_id, pdf_md5)

    for page_num in range(1, total_page + 1):
        page = doc[page_num - 1]
        objs = page.get_page_objects()
        page_text = page.get_text().strip()
        text_len = len(page_text)
        has_text_obj = any(obj.type == fitz.PDF_OBJECT_TEXT for obj in objs)
        has_image_obj = any(obj.type == fitz.PDF_OBJECT_IMAGE for obj in objs)

        # 视觉触发规则保留
        if has_image_obj and text_len < 100:
            vision_need_page_nums.append(page_num)

        if has_text_obj and has_image_obj:
            pdf_type = "mix"
        elif has_image_obj and not has_text_obj:
            pdf_type = "scan_pdf"

    return {
        "pdf_type": pdf_type,
        "vision_need_pages": vision_need_page_nums,
        "page_image_map": page_image_map,
        "total_page": total_page
    }
```

##### 阶段1：统一图片提取函数 `extract_images_from_pdf`（保留）

存储路径：`data/extracted_images/{user_id}/{md5}/p{page_num}_i{img_idx}.{ext}`（相对路径，由 `path_tool.get_data_path()` 解析为绝对路径）

执行细节：
1. PyMuPDF获取图片xref，提取原始字节流，保留原图格式；
2. 单张图片损坏仅跳过当前图片，不阻断整页；
3. 磁盘空间不足降级：不再落地图片，仅内存渲染，元数据清空image_paths；
4. 返回 `{页码: [图片路径列表]}`，用于RAG溯源。

---

#### 5.4.4 全链路统一标准处理流水线（精简大量冗余逻辑）

##### 通用前置统一步骤（所有PDF通用）

1. 异步任务读取缓存PDF，校验MD5完整性；
2. 调用`extract_images_from_pdf`提取并持久化内嵌图片；
3. 增强图层判定，区分文档类型、标记视觉页面；
4. **阶段2 页面渲染（简化）**
   - 统一固定 `Matrix(2,2)` 144dpi高清渲染，移除72dpi低分辨率分支；
   - 渲染失败降级为内存传图，不生成临时PNG文件；
5. **阶段3 感知哈希去重（仅页面级，移除图表哈希）**
   - 对高清页面截图计算pHash，汉明距离≤10归为一组；
   - 每组仅代表页调用多模态，同组页面复用视觉描述；
   - 哈希计算异常直接关闭去重，所有页面独立推理；
6. **阶段4 批量并发多模态调用（合并OCR链路，单套降级）**
   - 批量 `BATCH_SIZE=5`，`asyncio.gather`并发；
   - 仅一套并发信号量限制本地Ollama调用，移除独立OCR并发管控；
   - 主服务：阿里云百炼多模态LLM；故障自动切换本地Ollama；
   - 后台定时探测云端健康，恢复后自动切回云端；
   - 统一Prompt，强制 `--- Page N ---` 分隔符，正则拆分每页结果；
   - 降级链路简化：批量拆分→关闭并发→切本地Ollama→短路视觉链路；
7. 进入三类PDF差异化解析分支；
8. **阶段5 文本+视觉描述融合**，生成统一结构化Block；
9. 分页内容排序、切片、向量化入库，更新任务状态；
10. 成功清理缓存（小文件释放内存，大文件删除临时文件）；失败直接删除临时文件，提示重传。

##### 关键简化点说明

1. 删除独立云端/本地OCR整套链路，文字识别全部交给多模态模型；
2. 删除图表单独哈希去重，仅保留页面哈希；
3. 删除扫描件轮廓分割、区块分块推理，扫描件整页送入模型；
4. 删除视觉解析结果缓存，依靠页面哈希去重减少重复调用；
5. 取消双分辨率渲染分支，统一144dpi。

---

#### 5.4.5 三条差异化PDF分支（精简版）

##### 分支1：纯文本 PDF `text_pdf`（跳过视觉全流程）

触发条件：无图片、每页文本≥100字符

**执行步骤：**
1. PyMuPDF提取全文、标题、基础表格；
2. PyMuPDF提取异常则切换pdfplumber兜底；
3. 根据y轴坐标排序还原排版，生成纯文本Block。

**两级文本解析降级：**

| 优先级 | 策略 | 说明 |
|--------|------|------|
| 一级 | PyMuPDF 提取全文 | 主解析引擎 |
| 降级 1 | pdfplumber 兜底解析 | 乱码/空白时切换 |
| 最终兜底 | 文件损坏 | 全部失效，提示重传 |

##### 分支2：图文混合 PDF `mix`

**简化执行步骤：**
1. pdfplumber提取正文、原生表格、图表bbox坐标；
2. PyMuPDF根据bbox裁切图表局部区域（保留，减少图片token）；
3. 页面哈希去重完成，批量将页面截图+裁切图表送入多模态；
4. 模型同时识别文字、图表数据，按页面y坐标拼接：前文+图表描述+后文；

**降级链路（无独立OCR）：**

| 优先级 | 策略 | 说明 |
|--------|------|------|
| 一级 | 云端多模态统一解析图文 | 标准流程 |
| 降级 1 | 本地Ollama多模态兜底 | 云端限流/不可用切换 |
| 最终兜底 | 仅保留原生文本 | 多模态全部失效 |

##### 分支3：扫描 PDF `scan_pdf`（大幅简化图像逻辑）

触发条件：无原生TEXT对象，仅图片

**简化执行步骤：**
1. PyMuPDF渲染144dpi整页图片；
2. OpenCV基础预处理：灰度、二值化、倾斜矫正、裁白边、降噪；
3. 页面哈希去重，重复页面仅处理代表页；
4. **移除轮廓分割、区块拆分**，整页图片送入多模态统一识别文本与表格；
5. 复用代表页解析结果给同组重复扫描页；

**降级链路：**

| 优先级 | 策略 | 说明 |
|--------|------|------|
| 一级 | 云端多模态识别整页扫描图 | 标准流程 |
| 降级 1 | 本地Ollama多模态兜底 | 云端故障切换 |
| 最终兜底 | 占位文本`[本页扫描图像识别失败]` | 保证文档结构完整 |

---

#### 5.4.6 阶段5：统一结构化封装（精简元数据）

删减冗余视觉标记，保留RAG核心字段：

```json
{
  "page_num": 1,
  "block_type": "text/table/chart/image",
  "bbox": ["x1", "y1", "x2", "y2"],
  "content": "原生文本\n\n[页面视觉描述]: xxx",
  "level": "一级标题/二级标题/正文",
  "metadata": {
    "pdf_md5": "文件md5",
    "source_file": "原始文件名",
    "image_paths": ["data/extracted_images/xxx/p1_i0.png"],
    "has_images": true,
    "vision_source": "aliyun_llm / ollama",
    "page_phash": "页面哈希",
    "dedup_group_id": "去重分组ID，无则空",
    "downgrade_flag": false
  }
}
```

**文本融合规则（不变）：**
1. 原生文本 + 视觉描述：两段拼接；
2. 仅扫描图：仅使用视觉描述；
3. 纯文本：只保留原生文字。

输出按页码升序的Block列表，可直接转为LangChain Document。

---

#### 5.4.7 下游 RAG 衔接实现（无改动）

1. 标准化文本分块，配置块长度与重叠窗口；
2. Embedding生成向量；
3. 文本+向量+元数据存入向量库；
4. 检索可通过`image_paths`、页码溯源原图图表。

---

#### 5.4.8 全局工程兜底（精简缓存、监控、熔断）

##### 简化缓存体系（两层缓存，删除视觉结果缓存）

1. PDF原始文件缓存：解析过程临时使用，失败直接删除；
2. 提取图片持久缓存：同文件重复上传无需重新提取图片；

##### 资源超限熔断（简化阈值）

仅保留单文件大小阈值30MB：超大文件直接拒绝上传；
内存/队列过载：终止任务，返回「资源不足，请分批上传」；
磁盘不足：关闭图片落地存储。

##### 监控埋点简化

仅记录核心失败日志、多模态降级次数；移除细分并发峰值、图表哈希、OCR相关监控指标。

---

#### 5.4.9 全流程总览

1. 用户上传PDF → 小文件 (<10MB) 读入内存，大文件 (10-30MB) 流式写入临时缓存，返回task_id，后台异步消费；
2. 前置校验：加密/损坏文件直接报错；
3. 阶段1：提取PDF内嵌图片，本地持久化存储；
4. 增强图层判定，区分三类PDF，标记需要视觉处理的页面；
5. 统一144dpi渲染视觉页面截图；
6. 阶段3：页面感知哈希去重，减少多模态调用；
7. 阶段4：批量并发多模态（云端优先，自动切本地Ollama）；
8. 差异化解析：纯文本轻量化提取 / 图文裁切图表 / 扫描件基础图像预处理后整页识别；
9. 阶段5：图文内容融合，输出标准化结构化Block；
10. 文本分块、向量化入库，更新任务状态；
11. 成功清理缓存（小文件释放内存，大文件删除临时文件）；失败删除临时文件，提示用户重新上传。

#### 面试简述落地思路

整体采用异步缓存架构接收PDF，先用PyMuPDF统一提取所有内嵌图片并持久化；通过增强图层判定区分纯文本、图文混排、扫描件三类文档，同时依据「有图+文本不足100字符」精准筛选需视觉处理页面；所有待识图页面通过感知哈希去重降低多模态成本，采用5张批量并发调用多模态，支持阿里云云端/Ollama本地双后端自动降级；图文文档裁切局部图表而非整页投喂，扫描件经过OpenCV基础图像预处理后整页识别；移除独立OCR链路，全部图文识别统一交由多模态模型处理；统一输出带图片哈希、原图路径、视觉来源的结构化数据，无缝对接RAG向量入库，兼顾解析精度、调用成本与线上工程稳定性。

### 5.5 文本切分策略

#### 配置

```yaml
chunk_size: 500
chunk_overlap: 50
separators: ["\n\n", "\n", "。", "！", "？", "!", "?", " ", ""]
```

#### 切分器：RecursiveCharacterTextSplitter

按分隔符优先级递归尝试切分：

```
"\n\n"  →  先按段落切分
  ├── 子块 > 500字符？ → "\n" 按行切分
  │     ├── 子块 > 500字符？ → "。" 按中文句号切分
  │     │     ├── 子块 > 500字符？ → "！" 
  │     │     │     └── ... → "" 逐字符切分（兜底）
```

#### 两阶段策略

```
阶段 1: 递归字符切分（必执行）
  RecursiveCharacterTextSplitter.split_documents()
  → 每个 chunk <= 500 字符

阶段 2: 语义合并优化（可选，仅 split_text 时启用）
  计算相邻 chunk 余弦相似度
  相似度 > 0.7 → 合并（属于同一主题）
  防止关键语义被切断
```

**注意**：`processor.py` 中调用的是 `split_documents`，**不会触发语义合并**。这是为了保护 Document 的 metadata（page、image_paths）不被跨页合并破坏。

#### 为何 chunk_size=500？

- 500 字符 ≈ 150-250 tokens，属于**中等 chunk**
- 优势：兼顾检索精度与上下文完整性，单 chunk 即可承载完整段落
- 劣势：略增噪声，但通过重排序可有效过滤
- 补偿：chunk_overlap=50 确保段落边界不被切断

### 5.6 向量库写入策略

#### ChromaDB 配置

```yaml
collection_name: rag_collection
persist_directory: data/chromadb        # 由 path_tool.get_data_path("chromadb") 解析
k: 3
```

#### 单例模式

`VectorStoreService` 采用**双重检查锁定**实现线程安全单例：

```python
def __new__(cls):
    if cls._instance is None:          # 第一重检查（无锁，快速路径）
        with cls._init_lock:
            if cls._instance is None:  # 第二重检查（加锁，安全）
                cls._instance = super().__new__(cls)
    return cls._instance
```

**为什么是单例？** ChromaDB 0.5.x+ 的 `SharedSystemClient` 维护全局 `_instance` 缓存，多个实例会导致已销毁 client 的 KeyError。每次初始化前主动调用 `SharedSystemClient.clear_system_cache()`。

#### Metadata 策略

每个 chunk 携带的 metadata 及用途：

```python
doc.metadata = {
    "user_id": "user_001",              # → 检索过滤隔离
    "md5": "abc123def...",              # → 删除定位 + 图片 URL 反查
    "original_filename": "报告.pdf",      # → 前端展示 + 按文件聚合
    "page": 3,                           # → 检索结果页码标注
    "source": "C:\\Temp\\tmp123.pdf",     # → 原始路径
    "image_paths": ["p2_i0.png"],        # → 拼接图片 URL
    "has_images": True,                  # → 前端判断是否展示图片
    "created_at": "2026-06-14T10:30:00", # → 排序
}
```

#### 写入流程

```
切分后的 Document 列表
    → 注入 user_id, original_filename, md5
    → ChromaDB.add_documents(documents)  ← 写入
    → MD5Store.save_md5_hex(...)         ← MD5 记录持久化
```

### 5.7 MD5 去重机制

#### 去重流程

```
上传文件
    │
    ▼
计算文件 MD5 → 查询 MD5Store
  ├── 已存在 → 跳过整个文件
  └── 不存在 → 继续处理 → 写入完成后保存 MD5
```

#### 存储结构

```
data/md5_hex_store/
├── user_md5/
│   └── {user_id}/
│       └── md5_hex_store.txt    # JSON Lines 格式
└── public_md5/
    └── md5_hex_store.txt
```

#### 记录格式

```json
{"md5": "abc123...", "filename": "tmp_xxx.pdf", "original_filename": "年度报告.pdf", "upload_time": "2026-06-14T10:30:00"}
```

#### 设计选型：为什么不用 ChromaDB metadata 存 MD5？

- ChromaDB 删除后无法再获取已删除的 MD5
- 独立文件可保留完整历史
- JSON Lines：逐行追加/读取，内存友好
- 按用户隔离：用户间互不影响

#### 为什么 MD5 就够了？

| 场景 | MD5 能否拦截 | 分析 |
|------|-------------|------|
| 同一文件重复上传 | ✅ | 文件哈希完全一致，直接拦截 |
| 同一文件改名上传 | ✅ | MD5 基于文件内容计算，与文件名无关 |
| 不同文档含相同段落 | ❌ 不拦截 | 实际场景极少发生；即使发生，向量检索时相似 chunk 都会召回，少量冗余反而增强检索权重 |
| 同一文档稍作修改再上传 | ❌ 不拦截 | 用户主动上传修改版，理应作为新文件处理 |

> 在知识库场景中，用户极少故意上传内容高度重叠的不同文件。文件级 MD5 已覆盖 >99% 的重复场景，无需引入 chunk 级内容指纹增加计算和存储开销。

### 5.8 压缩包上传与并行解析 — 优化版

支持用户上传 `.zip` / `.tar.gz` 压缩包，后台异步解压后通过**全局公共复用文档管道**并行处理子文件，单文件失败不中断整包。

#### 5.8.1 架构设计

压缩包上传分为**外层独有逻辑**和**全局公共复用文档管道**两部分：

**外层独有逻辑（zip_handler.py）**：解压、扫描、并行调度、结果聚合、缓存刷新
**全局公共复用文档管道（_process_file_through_shared_pipeline）**：两条上传链路完全共用

```
┌─ Zip压缩包上传（独有外层逻辑）──────────────┐
│ 总包校验：后缀白名单 + ≤ 300MB              │
│ 创建 zip_batch_task_id，状态 pending        │
│ 解压至隔离临时目录 data/tmp/zip_{task_id}/   │
│ 递归扫描过滤有效子文件                      │
│   ├── 解压后总和 ≤ 200MB → 超限直接失败     │
│   └── 不支持的格式 → 计入 skipped           │
│                                              │
│ 线程池并发 → 逐个进入【全局公共复用文档管道】│
│   └── _process_file_through_shared_pipeline │
│       ├── ① MD5 全局查重                    │
│       ├── ② 文件格式分流 (PDF/普通)          │
│       ├── ③ 统一文本清洗                    │
│       ├── ④ 切片处理                        │
│       ├── ⑤ 向量入库(全局并发写信号量)      │
│       └── ⑥ 写入 MD5 入库记录              │
│       → 返回 FileProcessResult              │
│                                              │
│ 聚合统计 total/success/skipped/failed       │
│ 任一成功 → HybridRetriever.invalidate_cache │
│ 整体删除解压临时目录                        │
│ 更新批量任务为 completed                    │
└──────────────────────────────────────────────┘
```

| 原则 | 说明 |
|------|------|
| **全局公共复用文档管道** | `_process_file_through_shared_pipeline()` 同时服务单文件和压缩包两条链路，零重复代码 |
| **共享 DocumentProcessor** | `_get_shared_processor()` 懒加载单例，线程池中复用同一实例 |
| **单文件失败不中断整包** | `asyncio.gather(return_exceptions=True)` 保障 |
| **异步后台处理** | 压缩包上传后立即返回 `task_id`，前端轮询获取进度 |
| **结构化错误反馈** | 每个子文件返回 `FileProcessResult`，聚合为 `error_details` |
| **缓存一致性** | 任一文件成功入库后统一刷新 BM25 缓存 |

#### 5.8.2 上传接口

```
POST /api/knowledge/upload_zip
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `file` | UploadFile | ✅ | 压缩包文件（.zip / .tar.gz） |
| `user_id` | str | ✅ | 用户 ID |

**响应（立即返回 200）**：

```json
{
    "code": 200,
    "data": {
        "task_id": "zip_abc123def456",
        "status": "pending",
        "message": "压缩包已接收，正在后台处理"
    }
}
```

#### 5.8.3 任务查询接口

```
GET /api/knowledge/task/{task_id}
```

**响应（处理中）**：

```json
{
    "code": 200,
    "data": {
        "task_id": "zip_abc123def456",
        "status": "processing",
        "progress": {"total": 15, "success": 8, "skipped": 1, "failed": 0, "pending": 6}
    }
}
```

**响应（处理完成）**：

```json
{
    "code": 200,
    "data": {
        "task_id": "zip_abc123def456",
        "status": "completed",
        "progress": {"total": 15, "success": 12, "skipped": 2, "failed": 1},
        "error_details": [
            {"file_path": "report/2023/财务分析.xlsx", "error_type": "unsupported_format", "reason": "不支持的文件格式: .xlsx"},
            {"file_path": "docs/损坏文件.pdf", "error_type": "parse_failed", "reason": "PDF 文件已损坏"},
            {"file_path": "images/logo.png", "error_type": "unsupported_format", "reason": "不支持的文件格式: .png"}
        ]
    }
}
```

#### 5.8.4 错误分类

| error_type | 含义 | 前端处理建议 |
|------------|------|-------------|
| `unsupported_format` | 文件格式不在允许列表中 | 提示「不支持该格式，可单独上传」 |
| `parse_failed` | 格式支持但解析过程出错 | 提示「文件可能已损坏，请重新上传」 |
| `duplicate` | MD5 重复，已存在知识库中 | 提示「该文件已上传，自动跳过」 |
| `empty_content` | 解析后无有效文本内容 | 提示「文件内容为空，已跳过」 |
| `size_exceeded` | 压缩包或解压后总大小超限 | 提示「文件过大，请分批上传」 |

#### 5.8.5 与单文件上传的配合

| 场景 | 接口 | 说明 |
|------|------|------|
| 批量导入 | `POST /api/knowledge/upload_zip` | 压缩包上传，后台并行解析 |
| 补传失败文件 | `POST /knowledge/add/single` | 复用现有单文件上传接口 |
| 查询进度 | `GET /api/knowledge/task/{task_id}` | 前端轮询，间隔 2s |

> 两条链路均通过 `_process_file_through_shared_pipeline()` 调用 `DocumentProcessor.process()`，共用全局公共复用文档管道。

```
┌────────────────────────────────────────────────┐
│  ✅ 压缩包导入完成                              │
│                                                │
│  📦 季度报告汇总.zip                            │
│  ┌──────────┬──────┬───────────────────────┐  │
│  │ 成功     │ 12   │ 已导入知识库           │  │
│  │ 跳过     │ 2    │ 格式不支持，自动跳过   │  │
│  │ 失败     │ 1    │ 解析失败，请单独重传   │  │
│  └──────────┴──────┴───────────────────────┘  │
│                                                │
│  ⚠️ 以下文件解析失败，请单独重新上传对应文件：  │
│  ┌──────────────────────────────────────────┐  │
│  │ report/2023/财务分析.xlsx                │  │
│  │   → 不支持的文件格式: .xlsx              │  │
│  │                                          │  │
│  │ docs/损坏文件.pdf                        │  │
│  │   → PDF 文件已损坏，无法打开              │  │
│  │                                          │  │
│  │ images/logo.png                          │  │
│  │   → 不支持的文件格式: .png               │  │
│  └──────────────────────────────────────────┘  │
│                                                │
│  [重新上传失败文件]  [关闭]                     │
└────────────────────────────────────────────────┘
```

#### 5.8.8 与单文件上传的配合

| 场景 | 接口 | 说明 |
|------|------|------|
| 批量导入 | `POST /api/knowledge/upload_zip` | 压缩包上传，后台解析 |
| 补传失败文件 | `POST /api/knowledge/upload` | 复用现有单文件上传接口 |
| 查询进度 | `GET /api/knowledge/task/{task_id}` | 前端轮询，间隔 2s |

> 用户无需重传整个压缩包，只需从 `error_details` 中获取失败文件路径，单独调用已有的单文件上传接口即可。

---

## 六、检索策略

### 6.1 查询改写与必要性分类器

对用户原始查询进行分类判断，决定是否需要改写，提升检索召回精度。

**设计思路**：分类器的核心目标是**只在改写能提升召回时才调 LLM，否则直接用原查询**。每一次 HyDE 调用都是一次 LLM 推理，有延迟和成本，能省则省。

#### 前置处理：原始 Query 清洗

在进入分类器之前，仅去除首尾空格，不做其他处理：

```python
def preprocess_query(query: str) -> str:
    """清洗原始 Query（仅去空格，不做其他处理）"""
    return query.strip().replace(' ', '')
```

| 清洗步骤 | 示例 | 说明 |
|----------|------|------|
| 去空格 | `" RAG vs LLM "` → `"RAGvsLLM"` | 去除首尾及中间空格，空格对分类无意义且干扰长度计算 |

**注意**：清洗仅用于分类器判断，**不修改原始 Query**。检索时仍然使用原始 Query（含标点、原文大小写和空格）。

---

#### 第一层：极简关键词自动判定

无自定义词库，纯特征判断，判定是否为纯关键词查询：

**判定规则**：同时满足下面 2 条 = 纯关键词

| 条件 | 规则 | 说明 |
|------|------|------|
| ① | 不含任何 Q_WORDS 和 PRO_WORDS | Q_WORDS = `什么、怎么、如何、为什么、哪、谁、多少、吗、呢、？、?、能不能、可不可以`；PRO_WORDS = `它、他、她、这个、那个、这些、那些、它的、他的、她的、这、那、上面、前面、刚才` |
| ② | 清洗后总字符长度 ≤ 6 | 6 字以内足以容纳绝大多数中文术语和缩写（如 "RAG"=3、"Transformer"=11 但英文小写后仍 >6，会走第二层） |

| 分支 | 条件 | 判定 | 检索策略 |
|------|------|------|------|
| **分支 1** | 同时满足 ① 和 ② | **纯关键词** | `need_rewrite = False`，**仅执行 BM25 检索**（关键词精确匹配为主，向量检索对极短术语增益有限） |
| **分支 2** | 不满足 ① 或 ② | **非纯关键词** | 进入第二层简化改写三规则 |

> **为什么纯关键词只走 BM25？** 极短术语（如 "RAG"、"LLM"）在向量空间中语义信号非常稀疏，向量检索容易引入噪声。BM25 基于词频-逆文档频率做精确匹配，对这类查询反而更可靠。省去向量检索也减少了一次嵌入计算开销。

---

#### 第二层：简化改写三规则

进入此层的查询**不满足关键词判定**（含疑问词/代词，或长度 > 6），按以下三类规则判断：

| 规则 | 条件 | 触发逻辑 | 原因 |
|------|------|----------|------|
| **规则 A** | 存在代词 PRO_WORDS | 含 `它、他、她、这个、那个、这些、那些、它的、他的、她的、这、那、上面、前面、刚才` | 多轮对话中用户说"它有什么缺点？"，缺乏上下文原 query 完全无法检索，HyDE 结合历史展开指代是唯一解法 |
| **规则 B** | 存在疑问词 Q_WORDS | 含 `什么、怎么、如何、为什么、哪、谁、多少、吗、呢、？、?、能不能、可不可以` | 用户提问是疑问句，而知识库文档是陈述句，两者在向量空间分布差异大，HyDE 把问题翻译成陈述句后匹配度大幅提升 |
| **规则 C（兜底）** | 多轮对话有历史上下文 且 `len(query) < 15` | `conversation_history` 非空 且长度 < 15 | 追问往往省略主语，作为兜底规则覆盖规则 A/B 未能捕获的简短追问（如"能详细说说吗"、"展开讲讲"） |

#### 改写判定逻辑

```
满足规则 A / B / C 任意一条  → need_rewrite = True
                                → LLM 改写 Query → BM25 + 向量混合检索

A / B / C 全不满足              → need_rewrite = False
                                → 原始 Query 直接走 BM25 + 向量混合检索
```

#### 不改写的情况

| 示例 | 路径 | 结论 | 原因 |
|------|------|------|------|
| `"RAG"` | 第一层分支1 | 不改写（仅BM25） | 纯关键词，无 Q/PRO，长度 ≤6 |
| `"LLM"` | 第一层分支1 | 不改写（仅BM25） | 纯关键词 |
| `"GDP增长率"` | 第一层分支1 | 不改写（仅BM25） | 纯关键词 |
| `"Transformer注意力机制原理"` | 第二层，ABC全不满足 | 不改写（BM25+向量） | 长度 >6 但语义完整陈述句，无 Q/PRO |
| `"LangChain EnsembleRetriever 权重融合源码"` | 第二层，ABC全不满足 | 不改写（BM25+向量） | 关键词密集，HyDE 反而可能稀释关键词 |
| `"2024年新能源汽车市场渗透率数据分析"` | 第二层，ABC全不满足 | 不改写（BM25+向量） | 关键词密集，陈述句 |

#### 需要改写的情况

| 示例 | 路径 | 结论 | 原因 |
|------|------|------|------|
| `"它是什么"` | 第一层分支2 → 规则A+B | 改写 | 含代词+疑问词 |
| `"它有什么缺点"` | 第一层分支2 → 规则A+B | 改写 | 含代词+疑问词 |
| `"小户型适合什么扫地机器人"` | 第一层分支2 → 规则B | 改写 | 疑问句需转陈述句 |
| `"前面提到的那个方案"` | 第一层分支2 → 规则A | 改写 | 含多个代词 |
| `"能详细说说吗"` | 第一层分支2 → 规则C | 改写 | 多轮追问，无 Q/PRO 但长度 <15

#### 改写方案：生成假设性文档（HyDE）

改写原理：RAG 中，模型生成**假设检索到的文档**，可以更好地匹配向量空间中的真实文档片段。相比补全疑问句，假设文档改写更贴近知识库中文档的表达风格，能大幅提升召回命中率。

##### Prompt 模板

> **统一管理**：HyDE Prompt 由 `prompt_loader.load("hyde", query="...", chat_history="...")` 加载，模板文件位于 `app/config/prompts/hyde.txt`。详见 [9.6 提示词模板管理](#96-提示词模板管理--prompt_loaderpy)。

```
System: 你是一个查询改写助手。根据对话历史和用户当前查询，生成一段可能出现在知识库中的假设性文档片段，用于向量检索匹配。

User:
对话历史：{chat_history}
用户当前查询：{query}

要求：
1. 结合对话历史，将省略、指代补充完整，生成一段陈述句形式的假设性文档
2. 假设性文档应模拟知识库中真实段落的表达风格，可合理扩展但不可编造事实
3. 长度控制在 100-200 字，与知识库 chunk 大小匹配
4. 仅输出改写结果，不要附带任何解释、前缀或标记
```

##### 改写流程

```
原始 Query
    │
    ▼
预处理：去空格
    │
    ▼
第一层：极简关键词判定
    ├── 条件①：不含 Q_WORDS 和 PRO_WORDS
    └── 条件②：清洗后长度 ≤ 6
    │
    ├── 分支1：同时满足①② → 纯关键词
    │   │  need_rewrite = False
    │   └── 仅 BM25 检索 → 结果
    │
    └── 分支2：不满足 → 进入第二层
        │
        ▼
第二层：简化改写三规则
    ├── 规则A：含 PRO_WORDS
    ├── 规则B：含 Q_WORDS
    └── 规则C：有历史上下文 且 len < 15
    │
    ├── 满足 A/B/C 任意一条 → need_rewrite = True
    │   │  LLM 改写 Query（HyDE 生成假设性文档）
    │   └── 改写结果走 BM25 + 向量混合检索
    │
    └── A/B/C 全不满足 → need_rewrite = False
        └── 原始 Query 走 BM25 + 向量混合检索
```

**检索策略总结**：

| 判定结果 | 检索方式 | 说明 |
|----------|----------|------|
| 纯关键词（第一层分支1） | **仅 BM25** | 极短术语向量信号稀疏，BM25 精确匹配更可靠 |
| 不改写（第二层，ABC全不满足） | **BM25 + 向量混合** | 语义完整的陈述句，混合检索互补 |
| 需要改写（第二层，满足ABC任一） | **BM25 + 向量混合**（改写后） | 疑问句/代词经 HyDE 转陈述句后做混合检索 |

#### 必要性分类器实现

```python
# 词表定义
Q_WORDS = ['什么', '怎么', '如何', '为什么', '哪', '谁',
           '多少', '吗', '呢', '？', '?', '能不能', '可不可以']
PRO_WORDS = ['它', '他', '她', '这个', '那个', '这些', '那些',
             '它的', '他的', '她的', '这', '那', '上面', '前面', '刚才']


def preprocess_query(query: str) -> str:
    """清洗原始 Query（仅去空格，不做其他处理）"""
    return query.strip().replace(' ', '')


def is_pure_keyword(query: str) -> bool:
    """第一层：极简关键词判定（无自定义词库，纯特征判断）"""
    cleaned = preprocess_query(query)

    # 条件①：不含任何 Q_WORDS 和 PRO_WORDS
    has_q = any(w in cleaned for w in Q_WORDS)
    has_pro = any(w in cleaned for w in PRO_WORDS)
    if has_q or has_pro:
        return False

    # 条件②：清洗后总字符长度 ≤ 6
    return len(cleaned) <= 6


def need_rewrite(query: str, conversation_history: list = None) -> bool:
    """
    判断是否需要调用 HyDE 改写
    两层结构：第一层关键词过滤 → 第二层简化三规则
    """
    # 第一层：纯关键词 → 不改写，仅 BM25
    if is_pure_keyword(query):
        return False

    cleaned = preprocess_query(query)

    # 第二层：简化改写三规则（任一满足则改写）
    # 规则 A：存在代词
    if any(w in cleaned for w in PRO_WORDS):
        return True

    # 规则 B：存在疑问词
    if any(w in cleaned for w in Q_WORDS):
        return True

    # 规则 C：多轮对话中简短追问（兜底）
    if conversation_history and len(cleaned) < 15:
        return True

    # 其余情况：语义完整，不改写
    return False
```

#### 决策矩阵

| 查询示例 | 清洗后长度 | Q | PRO | 第一层 | 第二层 | 结论 | 检索方式 |
|----------|:---:|:---:|:---:|--------|--------|------|------|
| `"RAG"` | 3 | ❌ | ❌ | 纯关键词 | — | **不改写** | 仅 BM25 |
| `"LLM"` | 3 | ❌ | ❌ | 纯关键词 | — | **不改写** | 仅 BM25 |
| `"GDP增长率"` | 5 | ❌ | ❌ | 纯关键词 | — | **不改写** | 仅 BM25 |
| `"它是什么"` | 4 | ✅ | ✅ | 非关键词 | 规则A+B | **改写** | BM25+向量 |
| `"它有什么缺点"` | 6 | ✅ | ✅ | 非关键词 | 规则A+B | **改写** | BM25+向量 |
| `"小户型适合什么扫地机器人"` | 12 | ✅ | ❌ | 非关键词 | 规则B | **改写** | BM25+向量 |
| `"前面提到的那个方案"` | 8 | ❌ | ✅ | 非关键词 | 规则A | **改写** | BM25+向量 |
| `"Transformer注意力机制原理"` | 13 | ❌ | ❌ | 非关键词 | ABC全不满足 | **不改写** | BM25+向量 |
| `"LangChain EnsembleRetriever 权重融合源码"` | 30+ | ❌ | ❌ | 非关键词 | ABC全不满足 | **不改写** | BM25+向量 |
| `"能详细说说吗"` | 6 | ❌ | ❌ | 纯关键词→* | 规则C | **改写** | BM25+向量 |

> *注：`"能详细说说吗"` — 清洗后 `"能详细说说吗"` 不含 Q_WORDS/PRO_WORDS（"吗"不在 Q_WORDS、"能"不在 PRO_WORDS），长度=6，被第一层判定为纯关键词直接返回 `False`。但这是多轮追问场景，应在调用时传入 `conversation_history`，让规则 C 兜底。**实际使用时建议将 `"吗"` 加入 Q_WORDS，见下方改进建议。**

#### 改进建议

| 问题 | 建议 |
|------|------|
| `"能详细说说吗"` 被第一层误判为关键词 | 将 `"吗"`、`"呢"`、`"吧"` 等句末语气词加入 Q_WORDS，或调整第一层判断：若 query 以语气词结尾则不算纯关键词 |
| 纯关键词仅 BM25 可能漏召回 | 若知识库中相关文档未包含该关键词（如同义词），BM25 会漏掉。可考虑：纯关键词仍走混合检索，但向量权重降至 0.2 |

#### 优势

| 优势 | 说明 |
|------|------|
| **低开销** | 规则分类器 O(1)，不改写时省掉一次 LLM 调用；纯关键词省掉一次向量嵌入计算 |
| **两层递进** | 第一层快速过滤关键词（BM25 only），第二层精准判断改写需求，逻辑清晰无冗余 |
| **精准触发** | 3 条规则覆盖改写能提升召回的全部场景，其余情况不浪费推理 |
| **高召回** | 假设性文档匹配向量空间分布，疑问句→陈述句转换提升匹配度 |
| **保护 BM25** | 不改写时原查询关键词直接送给 BM25，不会被 HyDE 稀释；纯关键词场景 BM25 独占，最大化关键词匹配优势 |
| **处理省略** | 有效解决对话中的代词指代、省略问题 |

---

### 6.2 混合检索架构

使用 LangChain `EnsembleRetriever` 融合 BM25 + 向量检索，**RRF 排名融合**，两路并行异步执行：

```
查询
    │
    ├──────────────────────────────────────┐
    │  asyncio.gather 两路并行               │
    │                                      │
    ▼                                      ▼
原查询 → BM25 检索                    改写查询 → 向量检索
   关键词精确匹配                        ChromaDB similarity_search
   缓存读取 (LRU)                        filter={'user_id': user_id}
   缓存失效 → 重建索引                   若未触发改写，用原查询
   返回 top_k 结果 + 排名                 返回 top_k 结果 + 排名
    │                                      │
    └──────────────────────────────────────┘
                    │
                    ▼
            RRF 排名融合 (k=60)
            score(doc) = Σ 1/(k + rank_i)
                    │
                    ▼
            合并去重 → 候选文档列表
```

**并行执行 + RRF 融合**：
```python
import asyncio

# 两路并行检索
bm25_results, vector_results = await asyncio.gather(
    bm25_retriever.ainvoke(original_query),   # BM25 → [(doc, score, rank), ...]
    vector_store.asimilarity_search(           # 向量 → [(doc, score, rank), ...]
        rewritten_query or original_query,
        filter={"user_id": user_id},
        k=top_k
    )
)

# RRF 融合
def rrf_fusion(bm25_results, vector_results, k=60):
    scores = {}
    for rank, (doc, _, _) in enumerate(bm25_results, start=1):
        scores[doc.id] = scores.get(doc.id, 0) + 1 / (k + rank)
    for rank, (doc, _, _) in enumerate(vector_results, start=1):
        scores[doc.id] = scores.get(doc.id, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)

merged = rrf_fusion(bm25_results, vector_results)
# 合并去重 → 进入重排序
```

**设计考量**：
- 原查询保留精确关键词，送给 BM25 做关键词匹配，不会被改写稀释
- 改写查询（HyDE 生成的假设性文档）是陈述句，与知识库向量空间分布一致，适合向量语义检索
- **RRF 排名融合**，不依赖分数绝对值，只依赖排名，BM25 和向量分数量级不同也不影响公平性
- **两路 `asyncio.gather` 并行执行**，检索延迟取 max(BM25, 向量) 而非 sum，减少端到端响应时间

**用户隔离**：
- 向量检索：`filter={'user_id': user_id}`
- BM25：按用户隔离索引缓存，只加载该用户的文档

#### BM25 索引缓存策略

| 策略 | 说明 |
|------|------|
| **缓存粒度** | 每个 user_id 一个 BM25 索引 |
| **存储位置** | 内存缓存（LRU，最多缓存 20 个活跃用户索引） |
| **失效触发** | 该用户新增/删除文档后，立即失效缓存 |
| **冷启动** | 首次检索该用户时一次性重建并缓存 |
| **淘汰** | LRU 淘汰，超过缓存上限时淘汰最久未用索引 |

**设计考量：**
- 大部分场景用户知识库变更频率低，索引缓存可极大降低 BM25 重建开销；
- 内存 LRU 缓存足够满足中小规模并发场景，无需持久化到磁盘；
- 文档变更即失效，一致性简单可靠，不需要增量更新复杂逻辑。
- user_id 为空：返回 `EmptyRetriever`（始终空结果）

### 6.3 重排序

#### 模型

**BGE-Reranker-v2-m3**（BAAI），基于 `sentence-transformers` CrossEncoder：

| 属性 | 值 |
|------|-----|
| 来源 | ModelScope 下载 |
| 运行 | CUDA(优先) / CPU |
| max_length | 512 tokens |
| 推理 | model.eval() + torch.no_grad() |

#### 流程

```
RRF 融合结果 (多个 chunk)
    → 构造 (query, doc) 对
    → CrossEncoder.predict(pairs, batch_size=1)
    → 每个 chunk 获得相关性分数
    → 按分数降序排列
    → 返回排序结果
```

#### 为什么需要重排序？

- 向量检索和 BM25 都是**粗筛**阶段（召回），RRF 融合解决分数不可比问题
- CrossEncoder 共同编码 query+doc，能更精准判断语义相关性
- BGE-Reranker-v2-m3 是中文重排序领域的 SOTA 开源模型

---

### 6.4 Agent 智能体编排

基于 LangChain 最新框架构建 Agent 层，实现检索增强推理与多步决策。

#### 架构

```
用户查询
    │
    ▼
Agent 入口 (agent_service.py)
    │
    ├── 工具注册 ──→ 知识检索工具 (RAGService)
    │               ├── 联网搜索工具 (WebSearch)
    │               ├── 文档摘要工具 (Summarizer)
    │               └── 自定义工具 (可扩展)
    │
    ├── 推理循环 (ReAct / Tool Calling)
    │   ├── Thought: 分析查询意图，选择工具
    │   ├── Action: 调用工具获取结果
    │   └── Observation: 评估结果，判断是否继续
    │
    └── 最终响应 ──→ 合成多步检索结果，生成自然语言回答
```

**Agent 创建（LangChain 0.3+ API）**：

```python
from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory

# 0. ChatModel 含自动降级：DeepSeek → ChatTongyi
from app.utils.factory import create_chat_model
llm = create_chat_model()  # 返回 RunnableWithFallbacks

# 1. 统一工具导入
from app.utils.path_tool import get_db_path
from app.utils.prompt_loader import PromptLoader

loader = PromptLoader()

# 1. 定义 LLM 与外部服务（由 AgentService 注入）
from langchain_core.language_models import BaseChatModel
from app.rag.web_search_service import WebSearchService

llm: BaseChatModel = ...          # 由 AgentService.__init__ 注入
web_search_service = WebSearchService()

# 2. 定义工具
from langchain_core.tools import tool
from app.rag.rag_service import RAGService

@tool
def knowledge_search(query: str) -> str:
    """从用户知识库中检索相关文档（HyDE 改写 + 混合检索 + 重排序 + 摘要）"""
    return RAGService().search(query)

@tool
def web_search(query: str) -> str:
    """联网搜索补充外部实时信息"""
    return web_search_service.search(query)

@tool
def summarize(doc_content: str) -> str:
    """对长文档内容进行摘要"""
    return llm.invoke(f"请总结以下内容：\n{doc_content}")

tools = [knowledge_search, web_search, summarize]

# 2. 构建 Prompt（含消息历史占位符）
prompt = ChatPromptTemplate.from_messages([
    ("system", loader.load("system")),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])

# 3. 创建 Agent
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    handle_parsing_errors=True,
)

# 4. 绑定消息历史（多轮对话记忆，使用 SQLite 持久化）
from app.memory.memory_service import ConversationMemoryService

memory_service = ConversationMemoryService(db_path=get_db_path("conversation.db"))

def get_session_history(session_id: str):
    return memory_service.get_message_history(session_id)

agent_with_history = RunnableWithMessageHistory(
    agent_executor,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history",
)

# 5. 流式调用
async for event in agent_with_history.astream_events(
    {"input": "对比我上传的两份年度报告"},
    config={"configurable": {"session_id": "user_001"}},
    version="v2",
):
    # 处理 event["event"]: "on_tool_start" / "on_tool_end" / "on_chat_model_stream"
    yield event
```

#### 核心组件

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| Agent 框架 | `langchain_classic.agents.create_tool_calling_agent` + `AgentExecutor` | LangChain 1.x 兼容层，支持 Tool Calling |
| LLM | DeepSeek 官网 (主) → ChatTongyi (自动降级) | `with_fallbacks()` 机制，调用失败自动切换 |
| 工具注册 | `@tool` 装饰器 + `StructuredTool` | 标准化工具定义，自动生成 JSON Schema |
| 记忆管理 | `RunnableWithMessageHistory` + `ChatMessageHistory` | LangChain 0.3+ 推荐的消息历史管理方式 |
| 流式输出 | `agent_executor.astream_events()` | 实时推送 Agent 推理步骤（Thought/Action/Observation） |

#### 工具链

| 工具 | 功能 | 输入 | 输出 |
|------|------|------|------|
| 知识检索 | 从用户知识库检索相关文档（HyDE + 混合检索 + 重排序 + 摘要） | 查询文本 | 相关文档列表 + 分数 |
| 联网搜索 | 补充外部实时信息 | 搜索关键词 | 搜索结果摘要 |
| 文档摘要 | 对长文档内容生成摘要 | 文档全文 | 摘要文本 |
| 多步推理 | 链式分解复杂问题 | 子问题列表 | 逐步推理结果 |

#### Agent 推理流程示例

```
用户: "对比我上传的两份年度报告，分析今年业绩增长的主要原因"

Agent 推理:
  Step 1 → 知识检索: "年度报告 业绩增长 原因"
  Step 2 → 文档摘要: 对检索到的两份报告关键段落摘要
  Step 3 → 多步推理: 对比两份摘要，提取增长因素
  Step 4 → 合成回答: 输出结构化对比分析

最终输出: 带引用来源的结构化分析报告
```

---

### 6.5 统一检索入口 — chat.py

系统对外**仅暴露一个检索对话入口** `POST /chat`，由 `chat_router.py` → `chat_service.py` 承接。前端无论是普通 RAG 查询还是多步 Agent 推理，均通过该接口完成。

#### 设计动机

| 动机 | 说明 |
|------|------|
| **单一入口** | 避免前端需区分 `/rag/search`、`/agent/chat`、`/conversation` 等多个端点，降低集成复杂度 |
| **Agent 统一编排** | 所有检索能力（知识库检索、联网搜索、文档摘要）封装为 Agent 工具，由 LLM 自主决策调用哪把工具 |
| **会话透明** | 前端只需传 `session_id`，后端自动加载历史上下文、追加新消息，对前端透明 |
| **流式输出** | 统一使用 SSE（Server-Sent Events）流式返回，前端一套解析逻辑即可适配所有场景 |

#### 接口定义

```
POST /chat
Content-Type: application/json

{
  "query": "对比我上传的两份年度报告，分析今年业绩增长的主要原因",
  "session_id": "uuid-xxxx",          // 会话 ID（新会话传 null，后端自动创建）
  "user_id": "default_user",          // 当前阶段固定默认用户
  "stream": true                      // 是否流式返回（默认 true）
}
```

#### 调用链路

```
POST /chat
    │
    ▼
chat_router.py
    │  解析请求 → 校验参数 → 路由到 chat_service
    ▼
chat_service.py
    │
    ├── 1. 会话管理
    │   │  session_id 为空？ → 创建新会话（UUID）
    │   │  session_id 存在？ → 加载历史上下文
    │   └── ConversationMemoryService.load_context(session_id)
    │
    ├── 2. Agent 编排
    │   │  构建 Agent（含工具链 + 历史上下文）
    │   │  │
    │   │  ├── 知识检索工具 (knowledge_search)
    │   │  │   └── 内部调用 rag_service.py
    │   │  │       ├── HyDE 查询改写
    │   │  │       ├── 混合检索（BM25 + 向量）
    │   │  │       └── 重排序
    │   │  │
    │   │  ├── 联网搜索工具 (web_search)
    │   │  │   └── 补充外部实时信息
    │   │  │
    │   │  └── 文档摘要工具 (summarize)
    │   │      └── 对长文档生成摘要
    │   │
    │   └── AgentExecutor.astream_events()
    │       ├── Thought → 分析意图，选择工具
    │       ├── Action → 调用工具（如 knowledge_search）
    │       ├── Observation → 评估结果，判断是否继续
    │       └── Final Answer → 合成最终回答
    │
    ├── 3. 流式输出
    │   │  SSE event 类型：
    │   │  ├── on_tool_start  → 前端显示"正在检索知识库..."
    │   │  ├── on_tool_end    → 前端显示检索结果摘要
    │   │  ├── on_chat_model_stream → 逐 token 流式输出回答
    │   │  └── on_agent_finish → 对话完成
    │   └── 每个 event 通过 StreamingResponse 实时推送
    │
    └── 4. 会话持久化
        │  ConversationMemoryService 追加本轮对话
        │  ├── HumanMessage(query)
        │  └── AIMessage(final_answer)
        │
        └── 写入 db/conversation.db
```

#### 核心代码结构

```python
# chat_router.py
import json
import uuid
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from app.router.chat_service import ChatService
from app.schemas.models import ChatRequest

chat_router = APIRouter(prefix="/chat", tags=["chat"])

@chat_router.post("")
async def chat(request: ChatRequest):
    """统一对话入口：Agent + RAG + 会话管理"""
    service = ChatService()
    return StreamingResponse(
        service.handle_chat(
            query=request.query,
            session_id=request.session_id,
            user_id=request.user_id,
        ),
        media_type="text/event-stream",
    )


# chat_service.py
import json
import uuid
from app.memory.memory_service import ConversationMemoryService
from app.rag.agent_service import AgentService
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

class ChatService:
    def __init__(self):
        self.memory_service = ConversationMemoryService()
        self.agent_service = AgentService()

    async def handle_chat(self, query: str, session_id: str | None,
                          user_id: str):
        # 1. 会话管理
        if not session_id:
            session_id = str(uuid.uuid4())
        history = self.memory_service.load_context(session_id)

        # 2. 构建 Agent（含知识检索工具）
        agent = self.agent_service.create_agent_with_history(
            session_id=session_id,
            chat_history=history,
        )

        # 3. 流式执行 Agent
        async for event in agent.astream_events(
            {"input": query},
            config={"configurable": {"session_id": session_id}},
            version="v2",
        ):
            yield f"data: {json.dumps(event)}\n\n"

        # 4. 持久化（Agent 内部通过 RunnableWithMessageHistory 自动完成）
```

#### 与现有模块的关系

```
chat_router.py / chat_service.py     ← 统一入口（本模块）
    │
    ├── agent_service.py             ← Agent 编排（6.4 节）
    │   ├── knowledge_search tool    ← RAG 检索工具
    │   │   └── rag_service.py       ← RAG 核心（HyDE + 检索 + 重排序）
    │   ├── web_search tool          ← 联网搜索工具
    │   └── summarize tool           ← 文档摘要工具
    │
    └── memory_service.py            ← 会话记忆（十二节）
        └── db/conversation.db       ← SQLite 持久化
```

#### 设计要点

| 要点 | 说明 |
|------|------|
| RAG 检索即工具 | RAG 的完整链路（HyDE 改写 → 混合检索 → 重排序）封装为 `knowledge_search` tool，Agent 按需调用 |
| 统一入口 | 前端仅需对接 `POST /chat`，无需感知底层是简单 RAG 还是多步 Agent 推理 |
| 会话透明 | `session_id` 贯穿全链路，`RunnableWithMessageHistory` 自动管理消息持久化 |
| SSO 流式 | 全链路 SSE 流式输出，Agent 推理步骤（Thought/Action/Observation）实时可见 |
| 可扩展 | 新增工具只需注册到 Agent 工具链，无需修改 chat 路由 |

---

## 七、删除与清理

删除操作是**三层联动**的：

```
删除一个文件
    │
    ├── 1. ChromaDB 向量删除
    │      vectors_store.delete(where={
    │          "$and": [{"user_id": user_id}, {"md5": md5}]
    │      })
    │      通过 metadata 精准过滤，不依赖自动生成 ID
    │
    ├── 2. MD5 记录删除
    │      md5_store.delete_single_md5(user_id, md5)
    │      从 txt 移除该行，文件为空时自动清理目录
    │
    └── 3. 磁盘图片清理
           delete_image_directory(user_id, md5)
           rmtree data/extracted_images/{user_id}/{md5}/
```

| API | 功能 | 清理范围 |
|-----|------|----------|
| DELETE /knowledge/md5/delete/{md5} | 删除单个文档 | ChromaDB + MD5记录 + 图片 |
| DELETE /knowledge/md5/clear | 清空用户全库 | ChromaDB + MD5记录 + 全部图片 |
| DELETE /knowledge/md5/{filename} | 按文件名删除 | 同上 |

---

## 八、容错与自我修复

| 场景 | 策略 |
|------|------|
| ChromaDB 初始化失败 | 自动 rmtree 整个目录 + 重建 |
| ChromaDB 缓存冲突 | 每次初始化前 clear_system_cache() |
| Embedding 模型未就绪 | _LazyEmbedding 延迟加载 |
| 文件加载为空 | 诊断兜底：魔数检测 → 空文件/损坏/格式不支持 → 提示用户重传 + 写入日志 |
| 文档切分为空 | 跳过，不写入 |
| 单文件处理异常 | 独立 catch，诊断兜底 → 提示用户重传 + 写入日志 |
| TXT 编码不匹配 | utf-8 → gbk 回退 |
| PDF 打开失败 | 捕获异常，诊断兜底 → 提示用户重传 + 写入日志 |
| 视觉模型返回空 | 用已有文本回退 |
| 视觉模型格式不匹配 | 三层容错：正则→均分→填充 |
| MD5/图片目录不存在 | 首次使用自动创建 |

### 8.1 全链路日志与实时监控

系统在每一个主要节点均打上日志，并通过 `StreamHandler` 输出到控制台，开发运维时可实时查看运行进程。采用双日志器架构，覆盖**文件上传 → 文档解析 → 文本切分 → 向量入库 → 检索 → 重排序 → 总结**全链路。

#### 8.1.1 日志基础设施

> **统一入口**：所有模块通过 `log_tool.get_logger(__name__)` 获取日志器，底层由 `logger_handler.py` 配置 Handler/Formatter。详见 [9.5 日志统一管理](#95-日志统一管理--log_toolpy)。

| 日志器 | 位置 | 输出目标 | 用途 |
|--------|------|----------|------|
| `setup_logger()` | [log_tool.py](file:///D:/Knowledge_rag_system/app/utils/log_tool.py) | 控制台 + 文件 | 全局日志系统初始化（仅 main.py 启动时调用一次） |
| `get_logger()` | [log_tool.py](file:///D:/Knowledge_rag_system/app/utils/log_tool.py) | 控制台 + 文件（logs/ 目录） | 业务模块获取日志器，双输出，命名空间隔离 |

**日志格式**：

```
2026-06-18 14:30:00 - app - INFO - [rag_service.py] - 【HyDE】开始处理查询: 小户型适合什么扫地机器人
```

**日志级别**：由 `.env` 中的 `LOG_LEVEL` 环境变量控制，默认 `INFO`。开发环境可设为 `DEBUG` 查看完整堆栈。

#### 8.1.2 各节点日志覆盖

```
┌─────────────────────────────────────────────────────────────────────┐
│ 节点 1：服务启动                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ ✅ SQLite 数据库初始化完成                                                │
│ ✅ 数据库会话管理器初始化完成                                         │
│ ✅ 日志系统初始化完成                                                │
│ 🔄 开始后台初始化...                                                  │
│ ✅ chat_model 初始化完成                                              │
│ ✅ embed_model 初始化完成                                             │
│ ✅ vision_model 初始化完成                                            │
│ ✅ ChromaDB 向量数据库初始化完成                                  │
│ ✅ 重排序模型检查完成 → 下载（需要时）→ 加载                          │
│ ✅ ReorderService 初始化完成                                          │
│ ✅ 后台初始化完成，耗时 XX.X 秒                                        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 2：文件上传 & 验证                                              │
├─────────────────────────────────────────────────────────────────────┤
│ 【文件上传】接收文件: xxx.pdf, 大小: XXXXKB, 用户: user_xxx          │
│ 【MD5计算】文件路径不存在 → 终止                                     │
│ 【MD5计算】读取文件出错 → 终止                                       │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 3：文档加载                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ 【向量数据库】文件 xxx.pdf 的md5值 abc123 已存在，跳过               │
│ 【向量数据库】开始加载文档: xxx.pdf                                   │
│ 【PDF加载】UnstructuredPDFLoader失败，尝试PyPDFLoader: <错误原因>     │
│ 【文本文件加载】使用编码 utf-8 加载出错 → 尝试 gbk                   │
│ 【WORD文件加载】加载文件出错: <错误原因>                              │
│ 【Markdown文件加载】加载文件出错: <错误原因>                          │
│ 【PPT文件加载】加载文件出错: <错误原因>                               │
│ 【向量数据库】文件加载内容为空，跳过                                  │
│ 【多模态PDF加载】文件不存在 / 打开PDF失败                             │
│ 【多模态PDF加载】渲染第N页失败                                        │
│ 【多模态PDF加载】处理完成: N 页（全部纯文本）                         │
│ 【多模态PDF加载】去重失败(跳过)                                       │
│ 【诊断兜底】文件: xxx | 大小: XXX | 魔数: XXXXXX                     │
│   Loader 失败: xxx → 逐条记录                                        │
│   诊断: corrupted/unsupported_format/unknown → 具体原因              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 4：文本切分 & 向量入库                                          │
├─────────────────────────────────────────────────────────────────────┤
│ 【向量数据库】开始切分文档: xxx.pdf                                   │
│ 【向量数据库】文件切分内容为空，跳过                                  │
│ 【向量数据库】开始存储向量: xxx.pdf，文档数量: N                     │
│ 【向量数据库】文件 xxx.pdf 的md5值 abc123 已保存                     │
│ 【向量数据库】文件处理时出错: <错误原因>                              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 5：检索                                                         │
├─────────────────────────────────────────────────────────────────────┤
│ 【HyDE】开始处理查询: xxx                                            │
│ 【HyDE】生成的假设性文档: <全文>                                     │
│ 【HyDE】生成假设性文档失败: <错误原因>                                │
│ 【HyDE】user_id为空，不进行任何检索                                  │
│ 【HyDE】使用假设性文档进行检索                                        │
│ 【HyDE】检索到 N 个知识库文档                           │
│ 【HyDE】检索文档失败: <错误原因>                                      │
│ 【RAG】检索失败: <错误原因>                                       │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 6：重排序                                                       │
├─────────────────────────────────────────────────────────────────────┤
│ ✅ 加载重排序模型: /path/to/bge-reranker-v2-m3                       │
│ ✅ 模型加载成功，使用设备: cpu/cuda                                    │
│ ❌ 模型检查失败: <错误原因>                                           │
│ 【重排序服务】文档相似度分数: 0.XXXX                                  │
│ 【重排序服务】文档重排序成功，返回 N 个文档                           │
│ 【重排序服务】重排序失败: <错误原因>                                  │
│ 【RAG】文档重排序成功，返回 N 个文档                                  │
│ 【RAG】重排序失败: <错误原因>                                         │
│ 【RAG】user_id为空，不返回任何文档                                   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 7：RAG 总结                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ 【RAG】正在总结第N个文档                                              │
│ 【RAG】第N个文档总结耗时: X.XX秒                                      │
│ 【RAG】所有文档总结完成，总耗时: X.XX秒                               │
│ 【RAG】生成摘要成功                                                   │
│ 【RAG】合并摘要完成，开始生成最终总结                                  │
│ 【RAG】生成摘要超时                                                   │
│ 【RAG】生成摘要失败: <错误原因>                                       │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 8：删除 & 清理                                                  │
├─────────────────────────────────────────────────────────────────────┤
│ 【向量数据库】已删除用户 xxx 的所有文档                               │
│ 【向量数据库】已删除用户 xxx 的MD5记录                                │
│ 【向量数据库】文件 xxx 不存在于用户 xxx 的MD5记录中                  │
│ 【向量数据库】已删除用户 xxx 的文件 xxx 的MD5记录                    │
│ 【向量数据库】已删除用户 xxx 中文件 xxx 对应的文档                   │
│ 【向量数据库】删除出错: <错误原因>                                    │
│ 【向量数据库】获取MD5信息出错: <错误原因>                             │
│ 【向量数据库】获取用户 xxx 的知识库文档，共 N 个文件                  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ 节点 9：Agent 中间件（预留）                                          │
├─────────────────────────────────────────────────────────────────────┤
│ 通过 agent_middleware.py 注册 LangChain Agent 中间件                  │
│ before_agent / after_agent / before_model / after_model              │
│ wrap_model_call / wrap_tool_call                                     │
│ 每个生命周期节点均有日志输出，用于追踪 Agent 推理链路                 │
└─────────────────────────────────────────────────────────────────────┘
```

#### 8.1.3 日志标签规范

| 标签 | 所属模块 | 示例 |
|------|----------|------|
| `【HyDE】` | 查询改写 + 检索 | `【HyDE】开始处理查询: xxx` |
| `【RAG】` | 总结 + 端到端流程 | `【RAG】生成摘要成功` |
| `【向量数据库】` | 入库 + 切分 + 删除 | `【向量数据库】开始存储向量` |
| `【重排序服务】` | 重排序模型 | `【重排序服务】文档重排序成功` |
| `【PDF加载】` | PDF 文本解析 | `【PDF加载】PyPDFLoader失败` |
| `【多模态PDF加载】` | PDF 多模态解析 | `【多模态PDF加载】处理完成` |
| `【文本文件加载】` | TXT 加载 | `【文本文件加载】编码 utf-8 出错` |
| `【WORD文件加载】` | DOCX 加载 | `【WORD文件加载】加载出错` |
| `【Markdown文件加载】` | MD 加载 | `【Markdown文件加载】加载出错` |
| `【PPT文件加载】` | PPTX 加载 | `【PPT文件加载】加载出错` |
| `【MD5计算】` | 文件哈希 | `【MD5计算】读取文件出错` |
| `【文件列表】` | 目录操作 | `【文件列表】目录路径不存在` |
| `【诊断兜底】` | 加载失败诊断 | `【诊断兜底】文件: xxx` |

#### 8.1.4 控制台实时输出

所有日志通过 `StreamHandler` 同步输出到控制台（stdout），在 uvicorn 启动后可直接在终端中实时观察：

```
uvicorn main:app --reload
    │
    ▼
INFO:     Uvicorn running on http://127.0.0.1:8000
2026-06-18 14:30:00 - app - INFO - SQLite 数据库初始化完成
2026-06-18 14:30:00 - app - INFO - 数据库会话管理器初始化完成
2026-06-18 14:30:00 - app - INFO - 日志系统初始化完成
2026-06-18 14:30:01 - app - INFO - 🔄 开始后台初始化...
2026-06-18 14:30:02 - app - INFO - ✅ chat_model 初始化完成
2026-06-18 14:30:03 - app - INFO - ✅ embed_model 初始化完成
2026-06-18 14:30:03 - app - INFO - ✅ vision_model 初始化完成
2026-06-18 14:30:04 - app - INFO - ✅ ChromaDB 向量数据库初始化完成
2026-06-18 14:30:04 - app - INFO - ✅ 重排序模型检查完成
2026-06-18 14:30:05 - app - INFO - ✅ ReorderService 初始化完成
2026-06-18 14:30:05 - app - INFO - ✅ 后台初始化完成，耗时 4.2 秒
───────────────────────────────────────────────────────────────────
[用户上传文件]
2026-06-18 14:31:00 - app - INFO - 【向量数据库】开始加载文档: 年度报告.pdf
2026-06-18 14:31:01 - app - INFO - 【多模态PDF加载】处理完成: 12 页（全部纯文本）
2026-06-18 14:31:01 - app - INFO - 【向量数据库】开始切分文档: 年度报告.pdf
2026-06-18 14:31:02 - app - INFO - 【向量数据库】开始存储向量: 年度报告.pdf，文档数量: 45
2026-06-18 14:31:05 - app - INFO - 【向量数据库】文件 年度报告.pdf 的md5值 abc123 已保存
───────────────────────────────────────────────────────────────────
[用户发起检索]
2026-06-18 14:32:00 - app - INFO - 【HyDE】开始处理查询: 小户型适合什么扫地机器人
2026-06-18 14:32:01 - app - INFO - 【HyDE】生成的假设性文档: 小户型住宅由于空间有限...
2026-06-18 14:32:01 - app - INFO - 【HyDE】使用假设性文档进行检索
2026-06-18 14:32:02 - app - INFO - 【HyDE】检索到 5 个知识库文档
2026-06-18 14:32:02 - app - INFO - 【重排序服务】文档重排序成功，返回 3 个文档
2026-06-18 14:32:02 - app - INFO - 【RAG】文档重排序成功，返回 3 个文档
2026-06-18 14:32:02 - app - INFO - 【RAG】正在总结第1个文档
2026-06-18 14:32:03 - app - INFO - 【RAG】第1个文档总结耗时: 0.85秒
2026-06-18 14:32:03 - app - INFO - 【RAG】正在总结第2个文档
...
2026-06-18 14:32:06 - app - INFO - 【RAG】所有文档总结完成，总耗时: 3.45秒
2026-06-18 14:32:06 - app - INFO - 【RAG】生成摘要成功
```

#### 8.1.5 文件持久化存储

`get_logger()` 同时将日志写入 `logs/` 目录，按日志器名称和日期命名：

```
logs/
├── agent_20260618.log      # Agent 业务日志
├── rag_20260618.log        # RAG 检索日志
└── ...
```

**文件日志级别**：`DEBUG`（比控制台更详细，包含完整堆栈），便于事后排查。

**控制台日志级别**：`INFO`（避免刷屏，关键节点可见即可）。

#### 8.1.6 设计原则

| 原则 | 说明 |
|------|------|
| 全节点覆盖 | 从启动到检索的每个关键步骤均有日志输出，无盲区 |
| 标签区分 | 每个模块使用 `【模块名】` 前缀，便于快速定位日志来源 |
| 异常必录 | 所有 `except` 分支均记录 `logger.error`，包含异常原因和上下文 |
| 控制台可见 | 所有日志通过 `StreamHandler` 输出到控制台，开发运维时无需打开日志文件即可实时查看 |
| 文件持久化 | 业务日志同时写入 `logs/` 目录，文件级别 `DEBUG` 保留完整信息 |
| 级别可配 | 日志级别通过 `.env` 的 `LOG_LEVEL` 控制，生产环境可降为 `WARNING` 减少 I/O |

### 9.0 统一配置加载器 — loader.py

所有模块通过 `app/config/loader.py` 统一读取 `chroma.yaml`，缓存配置避免重复解析：

```python
from app.config.loader import get_config, load_chroma_config, reload_config

k = get_config("k", 3)                    # 读取单个配置项（点号分隔）
cfg = load_chroma_config()                # 获取完整配置字典
reload_config()                           # 强制热更新缓存
```

这也消除了各模块中散落的 `yaml.safe_load(open(...))` 重复代码。

### 9.1 chroma.yaml（完整版，30+ 配置项）

```yaml
# --- ChromaDB ---
collection_name: rag_collection
persist_directory: data/chromadb
hnsw_space: cosine
chromadb_telemetry: false

# --- 文件类型 ---
allow_knowledge_file_types: ["txt", "pdf", "md", "pptx", "docx"]
allowed_zip_extensions: [".zip", ".tar.gz", ".rar"]
text_encodings: ["utf-8", "gbk", "gb2312", "latin-1"]
mime_detect_buffer_size: 2048
allowed_mime_types: {application/pdf: pdf, text/plain: txt, ...}

# --- 魔数签名（诊断兜底） ---
magic_signatures: {"%PDF": [corrupted, ...], ...}

# --- 检索 ---
k: 3
rrf_constant: 60
vector_search_multiplier: 2
bm25_cache_size: 20

# --- 文本切分 ---
chunk_size: 500
chunk_overlap: 50
separators: ["\n\n", "\n", "。", "！", "？", "!", "?", " ", ""]
semantic_merge_threshold: 0.7

# --- 查询改写（词表 + 阈值） ---
pure_keyword_max_length: 6
short_query_max_length: 15
hyde_min_length: 3
history_max_chars: 200
query_words: ["什么", "怎么", ...]
pronoun_words: ["它", "他", ...]

# --- PDF 多模态 ---
vision_min_text_length: 100
dedup_hamming_distance: 10

# --- RAG 摘要 ---
summary_max_chars: 800
fallback_max_chars: 500
```

所有模块通过 `app.config.loader.get_config(key, default)` 统一读取，零硬编码。

### 9.2 核心环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| LLM_TYPE | DEEPSEEK | Chat 模型类型（DEEPSEEK / QWEN） |
| EMBED_MODEL_TYPE | ALIYUN | Embedding 类型（ALIYUN / OLLAMA） |
| VISION_MODEL_TYPE | ALIYUN | 视觉模型类型（ALIYUN / OLLAMA） |
| DEEPSEEK_API_KEY | — | DeepSeek 官网 API Key（优先，更便宜） |
| DEEPSEEK_MODEL | deepseek-chat | DeepSeek 官网模型名 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com/v1 | DeepSeek 官网 API 地址 |
| ALIYUN_ACCESS_KEY | — | 阿里云百炼 API Key |
| DEEPSEEK_ALIYUN_MODEL | deepseek-v4-pro | 百炼 DeepSeek 模型名（无官网 Key 时使用） |
| ALIYUN_BASE_URL | https://dashscope.aliyuncs.com/compatible-mode/v1 | 百炼 API 地址 |
| QWEN_MODEL_NAME | qwen3-max | ChatTongyi 模型名（降级备用） |
| ALIYUN_EMBED_MODEL | text-embedding-v4 | 阿里云 Embedding 模型 |
| OLLAMA_EMBED_MODEL | qwen3-embedding:0.6b | Ollama Embedding 模型 |
| ALIYUN_VISION_MODEL | qwen3.7-max-2026-06-08 | 阿里云视觉模型 |
| OLLAMA_VISION_MODEL | qwen-vl:7b | Ollama 视觉模型 |
| LLM_TEMPERATURE | 0.7 | LLM 温度参数 |
| OLLAMA_BASE_URL | http://localhost:11434 | Ollama 服务地址 |
| VISION_BATCH_SIZE | 5 | 多模态批次大小 |
| VISION_DEDUP_ENABLED | true | 感知哈希去重开关 |
| SCAN_RENDER_SCALE | 2 | 扫描 PDF 渲染缩放（2=144dpi） |
| RERANKER_MODEL_PATH | models/bge-reranker-v2-m3 | 重排序模型本地路径 |
| RERANKER_MODELSCOPE_NAME | BAAI/bge-reranker-v2-m3 | ModelScope 模型名（自动下载） |
| RERANKER_BATCH_SIZE | 1 | 重排序批次大小 |
| RERANKER_MAX_LENGTH | 512 | 重排序最大 token 数 |
| MAX_FILE_SIZE | 31457280 | 单文件上传限制（30MB） |
| MAX_ZIP_SIZE | 524288000 | 压缩包上传限制（500MB） |
| ZIP_MAX_WORKERS | 4 | 压缩包并行处理线程数 |
| SEMANTIC_MERGE_MODEL | paraphrase-multilingual-MiniLM-L12-v2 | 语义合并模型 |
| LOG_LEVEL | INFO | 日志级别 |
| LOG_DIR | logs | 日志输出目录 |
| MAX_MEMORY_TURNS | 10 | 会话记忆最大加载轮数 |
| TEXT_CLEAN_ENABLED | true | 文本清洗开关 |

### 9.3 会话记忆配置

会话记忆相关配置存放在 `.env` 环境变量中，与 ChromaDB 配置独立：

```yaml
# .env 中会话记忆相关配置
MAX_MEMORY_TURNS=10           # 每次加载最近 N 轮对话
CONVERSATION_DB_PATH=db/conversation.db  # 相对路径，由 path_tool.get_db_path() 解析为绝对路径
LOG_LEVEL=INFO                # 日志级别
LOG_DIR=logs                  # 相对路径，由 path_tool.get_logs_path() 解析为绝对路径
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `MAX_MEMORY_TURNS` | 10 | 每次检索时加载的历史对话轮数，避免超出 LLM 上下文限制 |
| `CONVERSATION_DB_PATH` | `db/conversation.db` | 单文件 SQLite 数据库，由 `path_tool` 统一解析为绝对路径 |
| `LOG_LEVEL` | INFO | 控制台 + 文件日志级别，开发环境可设为 DEBUG |
| `LOG_DIR` | logs | 日志文件存放目录，由 `path_tool` 统一解析为绝对路径 |

### 9.4 路径统一管理 — path_tool.py

项目中**所有文件路径**均由 `path_tool.py` 统一解析，禁止在业务代码中硬编码绝对路径。`path_tool` 以项目根目录为基准，将相对路径转换为绝对路径，保证跨环境部署的路径一致性。

#### 设计原则

| 原则 | 说明 |
|------|------|
| **单一基准** | 以项目根目录（`pyproject.toml` 所在目录）为唯一基准点 |
| **相对配绝对用** | 配置文件中存储相对路径，运行时由 `path_tool` 解析为绝对路径 |
| **零硬编码** | 禁止在业务代码中出现 `D:\xxx`、`/home/xxx` 等绝对路径字符串 |

#### 核心函数

```python
# app/utils/path_tool.py

from pathlib import Path

def get_project_root() -> Path:
    """返回项目根目录（pyproject.toml 所在目录）"""
    ...

def resolve_path(relative_path: str) -> Path:
    """将相对路径解析为绝对路径"""
    return get_project_root() / relative_path

def get_data_path(subpath: str = "") -> Path:
    """获取 data/ 目录下的绝对路径"""
    return resolve_path(f"data/{subpath}")

def get_db_path(filename: str = "") -> Path:
    """获取 db/ 目录下的绝对路径"""
    return resolve_path(f"db/{filename}")

def get_logs_path(subpath: str = "") -> Path:
    """获取 logs/ 目录下的绝对路径"""
    return resolve_path(f"logs/{subpath}")

def get_models_path(model_name: str = "") -> Path:
    """获取 models/ 目录下的绝对路径"""
    return resolve_path(f"models/{model_name}")
```

#### 使用示例

```python
from app.utils.path_tool import get_data_path, get_db_path, get_models_path

# ChromaDB 持久化目录
chroma_dir = get_data_path("chromadb")         # → D:/Knowledge_rag_system/data/chromadb

# 会话记忆数据库
db_file = get_db_path("conversation.db")        # → D:/Knowledge_rag_system/db/conversation.db

# 重排序模型
model_path = get_models_path("bge-reranker-v2-m3")  # → D:/Knowledge_rag_system/models/bge-reranker-v2-m3

# 日志目录
log_dir = get_logs_path()                       # → D:/Knowledge_rag_system/logs
```

#### 路径解析覆盖范围

| 模块 | 涉及路径 | 解析方式 |
|------|---------|---------|
| ChromaDB 向量存储 | `data/chromadb/` | `get_data_path("chromadb")` |
| MD5 去重存储 | `data/md5_hex_store/` | `get_data_path("md5_hex_store")` |
| PDF 图片提取 | `data/extracted_images/{user_id}/{md5}/` | `get_data_path("extracted_images/...")` |
| 压缩包解压临时 | `data/tmp/{task_id}/` | `get_data_path("tmp/{task_id}")` |
| 会话记忆 SQLite | `db/conversation.db` | `get_db_path("conversation.db")` |
| 日志文件 | `logs/{logger_name}/{date}.log` | `get_logs_path("{logger_name}/{date}.log")` |
| 重排序模型 | `models/bge-reranker-v2-m3/` | `get_models_path("bge-reranker-v2-m3")` |
| Prompt 模板 | `app/config/prompt.yaml` | `resolve_path("app/config/prompt.yaml")` |
| ChromaDB 配置 | `app/config/chroma.yaml` | `resolve_path("app/config/chroma.yaml")` |

### 9.5 日志统一管理 — log_tool.py

项目中**所有日志器**均由 `log_tool.py` 统一创建和配置，禁止在业务代码中直接调用 `logging.getLogger()` 或 `logging.basicConfig()`。`log_tool` 封装了底层 `logger_handler.py`，提供带命名空间隔离的日志器工厂，确保所有模块的日志输出格式一致、级别可控、双向输出（控制台 + 文件）。

#### 设计原则

| 原则 | 说明 |
|------|------|
| **统一入口** | 所有模块通过 `log_tool.get_logger(__name__)` 获取日志器，禁止直接使用 `logging.getLogger()` |
| **命名空间隔离** | 每个模块使用 `__name__` 作为日志器名称（如 `app.rag.rag_service`），自动按层级继承配置 |
| **双输出** | 控制台（INFO 级别，关键节点可见）+ 文件（DEBUG 级别，完整堆栈保留），级别由 `.env` 统一控制 |
| **零配置** | 业务模块只需调用 `get_logger(__name__)`，无需关心 Handler、Formatter 等底层配置 |
| **标签规范** | 所有日志消息使用 `【模块名】` 前缀，便于快速定位来源 |

#### 核心函数

```python
# app/utils/log_tool.py

import logging
from app.utils.path_tool import get_logs_path
from app.core.logger_handler import LogHandler

# 全局日志级别（由 .env 的 LOG_LEVEL 控制）
_LOG_LEVEL: str = "INFO"

def setup_logger(level: str = "INFO") -> None:
    """
    初始化全局日志系统（仅 main.py 启动时调用一次）
    - 设置根日志器级别
    - 配置控制台 Handler（StreamHandler，INFO 级别）
    - 配置文件 Handler（RotatingFileHandler，DEBUG 级别，按日期轮转）
    """
    global _LOG_LEVEL
    _LOG_LEVEL = level
    LogHandler.setup(
        console_level=level,
        file_level="DEBUG",
        log_dir=get_logs_path(),
    )

def get_logger(name: str) -> logging.Logger:
    """
    获取指定命名空间的日志器（各模块调用）
    - 自动继承全局 Handler 配置
    - 命名空间建议使用 __name__（如 'app.rag.rag_service'）
    """
    return logging.getLogger(name)

def get_all_loggers() -> dict[str, logging.Logger]:
    """获取所有已注册的日志器（用于调试）"""
    return logging.Logger.manager.loggerDict  # type: ignore
```

#### 使用示例

```python
# main.py — 启动时初始化
from app.utils.log_tool import setup_logger
setup_logger(level="INFO")

# 任意业务模块 — 获取日志器
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

# 按级别输出日志
logger.debug("【HyDE】假设性文档生成中...")          # 仅写入文件
logger.info("【HyDE】检索到 5 个知识库文档")          # 控制台 + 文件
logger.warning("【重排序】模型加载超时，使用默认排序")  # 控制台 + 文件
logger.error("【RAG】检索失败: ChromaDB 连接超时")     # 控制台 + 文件
```

#### 与 logger_handler.py 的关系

```
log_tool.py                     ← 统一入口（各模块调用）
    │
    └── logger_handler.py       ← 底层实现（Handler/Formatter 配置）
        ├── StreamHandler       ← 控制台输出（INFO 级别）
        └── RotatingFileHandler ← 文件输出（DEBUG 级别，按日期轮转）
            └── logs/
                ├── agent_20260618.log
                ├── rag_20260618.log
                └── ...
```

> **职责划分**：`log_tool.py` 提供统一 API 供业务模块调用；`logger_handler.py` 负责 Handler、Formatter 等底层配置。业务模块只依赖 `log_tool`，不直接接触 `logger_handler`。

#### 日志器命名规范

| 模块 | 日志器名称 | 日志文件 |
|------|-----------|---------|
| Agent 服务 | `app.rag.agent` | `logs/agent_YYYYMMDD.log` |
| RAG 检索 | `app.rag.rag_service` | `logs/rag_YYYYMMDD.log` |
| 文档处理 | `app.rag.document_handler` | `logs/rag_YYYYMMDD.log` |
| 知识库路由 | `app.router.knowledge` | `logs/agent_YYYYMMDD.log` |
| 会话管理 | `app.memory` | `logs/agent_YYYYMMDD.log` |
| 对话路由 | `app.router.chat` | `logs/agent_YYYYMMDD.log` |
| 压缩包处理 | `app.rag.zip_handler` | `logs/rag_YYYYMMDD.log` |

### 9.6 提示词模板管理 — prompt_loader.py

项目中**所有 Prompt 模板**均由 `prompt_loader.py` 统一加载和管理，禁止在业务代码中硬编码 Prompt 字符串。`prompt_loader` 从 `app/config/prompt.yaml` 读取模板路径配置，按需加载对应的 Prompt 文件，支持模板名称索引和运行时变量注入。

#### 设计原则

| 原则 | 说明 |
|------|------|
| **模板与代码分离** | Prompt 内容存储在独立的模板文件中，修改 Prompt 无需改动业务代码 |
| **统一加载** | 所有模块通过 `prompt_loader.load("template_name")` 获取 Prompt，禁止硬编码字符串 |
| **变量注入** | 支持 `{variable}` 占位符，运行时动态注入上下文变量 |
| **热更新友好** | 模板文件修改后，下次调用自动加载最新内容（非缓存模式） |

#### 核心函数

```python
# app/utils/prompt_loader.py

from pathlib import Path
from app.utils.path_tool import resolve_path

class PromptLoader:
    """Prompt 模板加载器：从 YAML 配置中读取模板路径，加载 Prompt 内容"""

    def __init__(self, config_path: str = "app/config/prompt.yaml"):
        self._config = self._load_config(resolve_path(config_path))
        self._cache: dict[str, str] = {}

    def load(self, name: str, **kwargs) -> str:
        """
        加载指定名称的 Prompt 模板
        - name: 模板名称（如 'system', 'hyde', 'agent'）
        - **kwargs: 运行时变量注入（如 user_id='xxx', context='...'）
        """
        template = self._read_template(name)
        return template.format(**kwargs) if kwargs else template

    def reload(self) -> None:
        """清空缓存，强制重新加载所有模板"""
        self._cache.clear()
        self._config = self._load_config(resolve_path("app/config/prompt.yaml"))
```

#### 默认系统 Prompt（system）

以下为系统默认的 RAG 检索助手 Prompt，存放在 `app/config/prompts/system.txt`：

```text
你是一个本地知识库检索助手，核心能力是帮助用户检索知识库内容来回答问题。你具备RAG能力可以检索用户上传的文档

你说话简单直接，不说废话。

## 核心任务
1. **优先检索原则**：回答任何问题前，**必须优先**使用 `rag_summary_tools` 从知识库中检索相关内容，基于检索结果进行回答
2. **RAG检索**：需要从知识库中获取详细知识时，使用 `rag_summary_tools` 检索并生成摘要
3. **直接回答**：只有在确认知识库中完全没有相关内容时，才对常识性问题直接回答

## 工具使用规则
1. 每次调用工具前，必须输出真实的自然语言思考过程
2. 思考过程完成后，直接触发工具调用，工具入参必须是合法的 JSON 格式，字符串值必须用双引号包裹，不能使用单引号
3. 参数中不要包含多余的换行符或非转义字符
4. 获取工具结果后，生成最终的自然语言回答，给出具体、实用的建议
5. 生成的结果要简单明了，少说废话
```

#### 使用示例

```python
from app.utils.prompt_loader import PromptLoader

loader = PromptLoader()

# 加载系统 Prompt
system_prompt = loader.load("system")

# 加载 HyDE Prompt（带变量注入）
hyde_prompt = loader.load("hyde", query="小户型适合什么扫地机器人")

# 构建 Agent Prompt
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

prompt = ChatPromptTemplate.from_messages([
    ("system", loader.load("system")),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])
```

#### Prompt 模板清单

| 模板名称 | 文件路径 | 用途 |
|---------|---------|------|
| `system` | `app/config/prompts/system.txt` | 系统级 Prompt：定义助手角色、核心任务、工具使用规则 |
| `hyde` | `app/config/prompts/hyde.txt` | HyDE 假设性文档生成 Prompt |
| `agent` | `app/config/prompts/agent.txt` | Agent 推理 Prompt（可覆盖 system 默认行为） |
| `summary` | `app/config/prompts/summary.txt` | 文档摘要 Prompt |
| `rewrite` | `app/config/prompts/rewrite.txt` | 查询改写 Prompt |

#### prompt.yaml 配置

```yaml
# app/config/prompt.yaml — Prompt 模板路径配置
templates:
  system:  app/config/prompts/system.txt
  hyde:    app/config/prompts/hyde.txt
  agent:   app/config/prompts/agent.txt
  summary: app/config/prompts/summary.txt
  rewrite: app/config/prompts/rewrite.txt
  vision:  app/config/prompts/vision.txt
```

---

## 十、其他改进建议

以下问题已在设计方案中解决，此处仅记录未纳入设计的待办项：

| 优先级 | 改进项 | 说明 |
|--------|--------|------|
| 中 | 支持更多格式 | CSV、HTML、EPUB 等格式的加载器扩展 |
| 低 | 原文高亮溯源 | 检索结果中高亮显示匹配关键词的原文片段 |

> **已解决的设计问题（不再列为改进项）：**
> - DOCX 加载器：已替换为 `Docx2txtLoader`（见 5.3 节）
> - 文本清洗：已新增通用文本清洗流水线（见 5.3 节后）
> - BM25 索引缓存：已新增 LRU 内存缓存机制（见 6.2 节）
> - LangChain 1.x 适配：已迁移至 `langchain_classic` 兼容层，版本约束对齐
> - 向量存储：已改用 `langchain_chroma.Chroma` 内置 API
> - 配置提取：50+ 硬编码项已全部迁移至 `.env` + `chroma.yaml`
> - Chat 降级：已使用 `with_fallbacks()` 实现 DeepSeek→ChatTongyi 自动切换
> - RAG 摘要：已改为 `ChatPromptTemplate | llm | StrOutputParser` LCEL 管线
> - 代码精简：移除死代码 `get_memory()`、手动转换 `_to_documents()` 等

---

## 十一、核心文件索引

| 文件 | 职责 | 关键类/函数 |
|------|------|-------------|
| app/rag/vector_store.py | langchain_chroma.Chroma 单例管理 | VectorStoreService |
| app/rag/document_handler/processor.py | 文档处理核心 + 诊断兜底 | DocumentProcessor.process() |
| app/rag/text_spliter.py | 文本切分 | AsyncTextSplitter |
| app/rag/retrievers/hybrid_retriever.py | BM25(LRU缓存)+向量并行+RRF融合 | HybridRetriever |
| app/rag/retrievers/query_rewriter.py | 两层分类器+HyDE改写（配置驱动） | get_retrieval_strategy(), hyde_rewrite() |
| app/rag/reorder_service.py | CrossEncoder 重排序（配置驱动） | ReorderService |
| app/rag/rag_service.py | RAG 核心（LCEL 摘要管线） | RAGService |
| app/rag/agent/agent_service.py | langchain_classic Agent 编排 | AgentService |
| app/rag/md5_manager/md5_store.py | MD5 JSON Lines 去重存储 | MD5Store |
| app/rag/zip_handler/zip_handler.py | 压缩包解压+并行解析+错误收集 | ZipTaskManager |
| app/memory/memory_service.py | SQLChatMessageHistory + SQLite | ConversationMemoryService |
| app/router/chat_router.py | 统一对话入口 | POST /chat |
| app/router/chat_service.py | 对话业务逻辑层 | ChatService |
| app/router/knowledge_router.py | 5 个知识库端点 | /knowledge/* |
| app/router/knowledge_service.py | 文件校验+三层联动删除 | KnowledgeService |
| app/router/conversation_router.py | 5 个会话端点 | /conversation/* |
| app/router/conversation_service.py | 会话管理 | ConversationService |
| app/router/zip_router.py | 压缩包上传+任务查询 | POST /api/knowledge/upload_zip |
| app/schemas/models.py | Pydantic 数据模型 | ChatRequest, TaskStatusResponse 等 |
| app/utils/factory.py | 模型工厂（with_fallbacks 自动降级） | create_chat_model(), create_embedding_model() |
| app/utils/file_handler.py | 多格式加载器（配置驱动） | load_file(), txt_loader() 等 |
| app/utils/pdf_multimodal_loader.py | PDF 三分支多模态解析 | pdf_multimodal_loader() |
| app/utils/image_extractor.py | PDF 图片提取 | extract_images_from_pdf() |
| app/utils/vision_service.py | 视觉服务（Prompt 模板化） | VisionService |
| app/utils/prompt_loader.py | Prompt 统一加载器 | PromptLoader |
| app/utils/path_tool.py | 路径统一管理 | get_data_path(), get_db_path() 等 |
| app/utils/log_tool.py | 日志统一管理 | get_logger(), setup_logger() |
| app/config/loader.py | 统一配置加载器 | get_config(), load_chroma_config() |
| app/core/background_init.py | 后台异步初始化 | _BackgroundInitManager |
| app/core/logger_handler.py | 日志 Handler 配置 | LogHandler.setup() |
| app/core/success_response.py | 统一成功响应 | success_response() |
| app/core/failed_response.py | 统一异常处理 | AppException, DocumentLoadException |
| app/config/chroma.yaml | 30+ 配置项 | 检索/切分/阈值/词表/MIME/魔数 |
| app/config/prompt.yaml | 6 个 Prompt 模板路径 | system/hyde/agent/summary/rewrite/vision |
| db/conversation.db | 会话记忆 SQLite | 全量对话记录 |

---

## 十二、会话记忆设计

### 12.1 设计目标

为 RAG 问答系统引入**多轮对话记忆能力**，使系统能够理解上下文指代（如"上一个问题"、"它"）、延续对话主题，并在多轮交互中保持连贯的推理状态。

### 12.2 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| 记忆框架 | LangChain Memory | 与现有 LangChain 生态无缝集成 |
| 存储后端 | SQLite | 轻量、零配置、单文件部署，适合本地知识库场景 |
| 存储策略 | 全量存储 | 保留完整对话历史，不截断、不摘要 |
| 存储路径 | `db/conversation.db` | 项目根目录下 db/ 目录，单文件数据库 |

### 12.3 架构设计

```
用户输入 Query
    │
    ▼
┌─────────────────────────────────────────────┐
│           ConversationMemory                │
│  从 SQLite 加载历史会话上下文                │
│  ┌─────────────────────────────────────┐    │
│  │ session_id: "user_abc"              │    │
│  │ messages:                           │    │
│  │   [HumanMessage("什么是RAG?"),       │    │
│  │    AIMessage("RAG是检索增强..."),    │    │
│  │    HumanMessage("它有什么优点?"),    │    │
│  │    AIMessage("RAG的主要优点..."),    │    │
│  │    ...]                             │    │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
    │
    ▼
拼接历史上下文 + 当前 Query → LLM 生成回答
    │
    ▼
┌─────────────────────────────────────────────┐
│  新消息追加到 SQLite                         │
│  INSERT INTO messages (session_id, role,    │
│    content, created_at) VALUES (...)         │
└─────────────────────────────────────────────┘
```

### 12.4 SQLite 数据表设计

系统采用双层表结构：**LangChain 自动管理的消息存储表** + **自定义会话元信息表**。

```sql
-- 会话元信息表：记录每个会话的概要信息
CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,          -- 会话唯一标识（UUID），与 message_store.session_id 对应
    user_id       TEXT NOT NULL,             -- 用户标识
    title         TEXT DEFAULT '',           -- 会话标题（首条用户消息前30字）
    created_at    TEXT NOT NULL,             -- 创建时间（ISO 8601）
    updated_at    TEXT NOT NULL              -- 最后更新时间（ISO 8601）
);

-- 消息存储表：由 LangChain SQLChatMessageHistory 自动创建和管理
-- 表名：message_store（LangChain 默认表名）
-- 结构：id INTEGER PRIMARY KEY, session_id TEXT, message JSON TEXT
-- 每条 message 为 LangChain Message 对象的 JSON 序列化，包含 type、content 等字段

-- 索引：加速会话消息查询
CREATE INDEX IF NOT EXISTS idx_message_store_session_id
    ON message_store(session_id);
```

> **设计说明**：`message_store` 表由 `SQLChatMessageHistory` 在首次调用时自动创建，无需手动 DDL。`conversations` 表为自定义扩展，存储会话级别的元信息（标题、创建时间等），通过 `session_id` 与 `message_store` 关联。

### 12.5 LangChain 集成方式

使用 `SQLChatMessageHistory` 实现 LangChain 标准消息历史接口：

```python
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain.memory import ConversationBufferMemory
from langchain_core.messages import HumanMessage, AIMessage
from app.utils.path_tool import get_db_path

class ConversationMemoryService:
    """会话记忆服务：管理多轮对话的存储与加载"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or get_db_path("conversation.db")

    def get_message_history(self, session_id: str) -> SQLChatMessageHistory:
        """获取指定会话的消息历史"""
        return SQLChatMessageHistory(
            session_id=session_id,
            connection_string=f"sqlite:///{self.db_path}",
        )

    def get_memory(self, session_id: str) -> ConversationBufferMemory:
        """创建带记忆的对话缓冲区"""
        history = self.get_message_history(session_id)
        return ConversationBufferMemory(
            chat_memory=history,
            return_messages=True,             # 返回 Message 对象而非字符串
            memory_key="chat_history",        # Prompt 模板中的占位变量名
        )

    def load_context(self, session_id: str, max_turns: int = 10) -> list:
        """加载最近 N 轮对话作为上下文"""
        history = self.get_message_history(session_id)
        messages = history.messages
        return messages[-(max_turns * 2):]    # 每轮含 human + ai 两条
```

### 12.6 检索链路集成

会话记忆与 RAG 检索链的集成方式：

```
用户输入 Query（含 session_id）
    │
    ▼
ConversationMemoryService.load_context(session_id)
    │  加载最近 N 轮历史消息
    ▼
┌─────────────────────────────────────────────┐
│  构建完整 Prompt                              │
│                                              │
│  System: 你是一个知识库助手...                 │
│  Chat History:                               │
│    Human: 什么是RAG?                          │
│    AI: RAG是检索增强生成...                    │
│    Human: 它有什么优点?                        │
│  Context: [检索到的知识库文档内容]             │
│  Human: 能详细说说吗?                          │
└─────────────────────────────────────────────┘
    │
    ▼
LLM 生成回答（结合历史上下文 + 检索结果）
    │
    ▼
ConversationMemoryService 追加本轮对话
    ├── HumanMessage(content="能详细说说吗?")
    └── AIMessage(content="当然，RAG的...")
    │
    ▼
SQLite 持久化写入 db/conversation.db
```

### 12.7 会话生命周期

| 阶段 | 操作 | 说明 |
|------|------|------|
| 对话 | `POST /chat` | **统一检索对话入口**，每次对话自动追加到 SQLite |
| 创建 | `POST /conversation/new` | 纯创建会话（前端也可直接传 `session_id: null` 到 `/chat` 自动创建） |
| 列表 | `GET /conversations?user_id=xxx` | 按更新时间倒序排列 |
| 历史 | `GET /conversation/{id}/messages` | 加载指定会话的全部消息 |
| 删除 | `DELETE /conversation/{id}` | 级联删除所有关联消息 |
| 清空 | `DELETE /conversations?user_id=xxx` | 清空某用户全部会话 |

> **检索入口说明**：`POST /chat` 是系统唯一的检索对话入口。RAG 检索能力已被封装为 `knowledge_search` 工具，由 Agent 在 `/chat` 路由中统一调度。详细说明见 [6.5 统一检索入口](#65-统一检索入口--chatpy)。

### 12.8 设计要点

| 要点 | 说明 |
|------|------|
| 全量存储 | 不做消息截断或摘要压缩，完整保留对话历史，便于审计和回溯 |
| 按用户隔离 | 每个 `user_id` 拥有独立的会话空间，互不可见。（当前阶段默认使用同一 `user_id`，后续可扩展多用户） |
| 上下文窗口控制 | 每次仅加载最近 N 轮（默认 10 轮），避免超出 LLM 上下文限制 |
| 级联删除 | 删除会话时自动清理关联消息，不留孤儿数据 |
| 单文件部署 | SQLite 单文件 `conversation.db`，无需额外数据库服务，与项目一起迁移 |
| 索引优化 | 对 `message_store(session_id)` 建立索引，加速历史消息查询 |

---

## 十三、实现与设计的差异说明

以下设计在实现时有简化，均属于合理取舍：

| 设计项 | 报告描述 | 实际实现 | 原因 |
|--------|---------|---------|------|
| 单文件异步上传 | <10MB 内存处理，>10MB 流式写入，返回 202 + task_id | 全同步处理，所有文件写入临时磁盘 | 后续可加异步模式 |
| SharedSystemClient.clear_system_cache() | ChromaDB 初始化前清缓存 | 未实现 | ChromaDB 1.5+ 架构变更，不再需要 |
| 限流 5次/分钟 | 单文件上传端点限流 | 未实现 | 后续迭代加限流中间件 |
| EnsembleRetriever | 使用 LangChain EnsembleRetriever 融合 | 自定义 RRF 融合实现 | 功能等价，自定义更灵活控制 |
| 多步推理工具 | 独立 `multi_step_reasoning` 工具 | 未实现 | LLM ReAct 循环自行分解复杂问题，无需独立工具 |
| `DocumentLoadException` | 诊断结果以异常形式抛出 | 以字典从 `process()` 返回 | 调用方统一处理，无需额外异常类型 |
| `public_md5` 目录 | 公共 MD5 存储与用户目录并列 | 仅有 `user_md5` | 当前无公共文档需求 |
| `model.eval() + torch.no_grad()` | 重排序显式推理模式 | `CrossEncoder.predict()` 内部处理 | sentence-transformers 自动管理推理模式 |