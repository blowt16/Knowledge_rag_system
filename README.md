# Knowledge RAG System

本地知识库 RAG 检索系统，支持文档上传、向量检索、Agent 工具链对话。

## 特性

- **双模式对话**：直接 RAG 检索（关键词 / 混合检索）和 Agent 工具链（knowledge_search + web_search + summarize）
- **混合检索**：BM25 关键词 + 向量相似度 + RRF 融合 + BGE-Reranker-v2-m3 重排序
- **HyDE 改写**：对自然语言问题生成假设性文档，增强向量检索匹配
- **文档管理**：支持 txt / pdf / md / pptx / docx 单文件上传及 zip / tar.gz / rar 批量上传
- **多模态 PDF**：扫描版 PDF 自动 OCR 提取，图文混排 PDF 视觉模型理解
- **会话管理**：多轮对话持久化，置顶、分页、历史回溯
- **Streamlit 前端**：对话主页 + 知识库管理页 + 会话管理页

## 技术栈

| 层 | 技术 |
|---|------|
| 后端框架 | FastAPI + uvicorn |
| 对话引擎 | LangChain (Agent + RAG Chain) |
| 向量数据库 | ChromaDB |
| 嵌入模型 | 阿里云 text-embedding-v4 / Ollama qwen3-embedding |
| LLM | DeepSeek / Qwen (阿里云百炼 / DeepSeek 官网 / Ollama) |
| 重排序 | BGE-Reranker-v2-m3 (CrossEncoder) |
| BM25 | rank-bm25 (jieba 分词) |
| 前端 | Streamlit |
| 文档解析 | PyMuPDF / pdfplumber / python-docx / python-pptx |

## 快速开始

### 前置条件

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/) 包管理器

### 安装

```bash
git clone https://github.com/blowt16/Knowledge_rag_system.git
cd Knowledge_rag_system

# 配置环境变量
cp .env.example .env   # 编辑 .env 填入 API Key
```

### 启动

```bash
# 后端 (端口 8000)
uv run uvicorn main:app --reload --port 8000

# 前端 (端口 8501)
uv run streamlit run front/app.py
```

启动后访问 `http://localhost:8501` 使用 Streamlit 前端，或 `http://localhost:8000/docs` 查看 API 文档。

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/chat` | 对话入口（SSE 流式），支持 Agent / RAG 双模式 |
| `POST` | `/knowledge/add/single` | 上传文档 |
| `GET` | `/knowledge/documents` | 文档列表 |
| `DELETE` | `/knowledge/md5/delete/{md5}` | 按 MD5 删除文档 |
| `DELETE` | `/knowledge/md5/clear` | 清空知识库 |
| `POST` | `/api/knowledge/upload_zip` | 批量上传压缩包 |
| `GET` | `/api/knowledge/task/{task_id}/stream` | 压缩包处理进度（SSE） |
| `POST` | `/conversation/new` | 创建会话 |
| `GET` | `/conversation/list` | 会话列表（分页） |
| `GET` | `/conversation/{id}/messages` | 获取会话消息 |
| `POST` | `/conversation/{id}/pin` | 置顶 / 取消置顶 |
| `DELETE` | `/conversation/{id}` | 删除会话 |
| `DELETE` | `/conversation/clear/{user_id}` | 清空用户会话 |

## 检索策略

### 查询分类 → 检索策略

| 查询类型 | 判定条件 | 检索策略 | 是否改写 |
|----------|---------|---------|:------:|
| 纯关键字 | ≤10 字符 + 无疑问词/代词 | BM25 only | ❌ |
| 多词关键字 | >10 字符 + 无疑问词/代词 | BM25 + 向量 + RRF | ❌ |
| 自然语言问题 | 包含疑问词/代词 | BM25 + 向量(HyDE) + RRF | ✅ HyDE |

### 检索流程

1. **策略判定** — 分析查询类型，选择检索策略
2. **HyDE 改写**（仅自然语言问题）— 生成假设性文档片段增强向量匹配
3. **混合检索** — BM25 关键词 + 向量相似度并行检索
4. **RRF 融合** — 倒数排名融合去重
5. **重排序** — BGE-Reranker-v2-m3 CrossEncoder 精排
6. **LLM 生成** — 基于检索结果流式生成回答

## Agent 模式

Agent 模式注册了三个工具：

- `knowledge_search` — 知识库检索（必须首先调用）
- `web_search` — 联网搜索补充信息
- `summarize_document` — 长文档摘要

系统提示词强制每轮对话先调用 `knowledge_search`，确保回答始终基于最新知识库内容。

## 配置

核心配置在 `app/config/chroma.yaml`：

```yaml
# 检索参数
k: 5                      # 重排序返回数量
rrf_constant: 60          # RRF 融合常数
vector_search_multiplier: 2 # 向量检索候选倍数

# 查询改写
pure_keyword_max_length: 10 # 纯关键字最大长度
hyde_min_length: 3          # HyDE 生成最小长度

# 文本切分
chunk_size: 500
chunk_overlap: 50
```

环境变量在 `.env` 中配置 LLM 类型、API Key、模型参数等。

## 项目结构

```
Knowledge_rag_system/
├── main.py                    # FastAPI 入口
├── app/
│   ├── agent/                 # Agent 编排服务
│   │   └── agent_service.py
│   ├── config/                # YAML 配置 + 提示词模板
│   │   ├── chroma.yaml
│   │   ├── prompt.yaml
│   │   └── prompts/
│   ├── core/                  # 初始化、异常处理、日志
│   │   ├── background_init.py
│   │   ├── failed_response.py
│   │   └── logger_handler.py
│   ├── memory/                # SQLite 会话持久化
│   │   └── memory_service.py
│   ├── rag/                   # RAG 检索管线
│   │   ├── rag_service.py
│   │   ├── vector_store.py
│   │   ├── reorder_service.py
│   │   └── retrievers/
│   ├── router/                # API 路由
│   │   ├── chat_router.py
│   │   ├── chat_service.py
│   │   ├── conversation_router.py
│   │   └── knowledge_router.py
│   ├── schemas/               # Pydantic 模型
│   └── utils/                 # 工具函数
├── front/                     # Streamlit 前端
│   ├── app.py                 # 对话主页
│   ├── api_client.py          # API 客户端
│   └── pages/
│       ├── 01_knowledge.py    # 知识库管理
│       └── 02_conversations.py # 会话管理
├── data/                      # 运行时数据
│   └── chromadb/              # 向量数据库持久化
├── db/                        # SQLite 会话数据库
├── logs/                      # 日志文件
└── models/                    # 本地模型
    └── bge-reranker-v2-m3/
```
