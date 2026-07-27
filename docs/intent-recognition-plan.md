# 意图识别模块实施方案

**版本**: v1.0  
**日期**: 2026-07-27  
**状态**: 待实施

---

## 1. 问题定义

### 1.1 现状

- 意图分类逻辑内嵌在 Agent 系统提示词（[agent.txt](file:///D:/Knowledge_rag_system/app/config/prompts/agent.txt)）中
- 由主 LLM（DeepSeek-V4-Pro）在推理时同时完成意图判定和答案生成
- 主 LLM 在多轮对话中有时会跳过 `knowledge_search`，直接基于历史作答

### 1.2 根因

| 根因 | 说明 |
|------|------|
| 单一 LLM 承担双重任务 | 意图判定 + 答案生成混在一个调用中，注意力分散 |
| 提示词约束力不足 | 主 LLM 倾向将"必须检索"视为"建议"而非"命令" |
| 多轮对话的 recency bias | 历史中已有答案片段时，LLM 倾向于复用而非重新检索 |
| 无独立前置判定 | 缺少轻量级的前置网关，无法在进入主流程前拦截 |

### 1.3 目标

- 在 query 进入主流程前，由**独立轻量 LLM** 完成意图分类
- 将分类结果转化为**硬约束指令**注入主 LLM，确保 `knowledge_search` 被稳定调用
- 闲聊/能力问询类请求**直接跳过 Agent 工具链**，节省 token 和延迟

---

## 2. 架构设计

### 2.1 总体架构

```
用户 query
    │
    ▼
┌─────────────────────────────────────────────────────┐
│               IntentClassifier                       │
│  ┌──────────────┐                                    │
│  │ 轻量 LLM     │  qwen-turbo / deepseek-chat-lite   │
│  │ (同步调用)   │                                    │
│  └──────┬───────┘                                    │
│         │                                             │
│         ▼                                             │
│  ┌──────────────────────────────────────────────┐    │
│  │ 返回结构化结果:                               │    │
│  │ {                                            │    │
│  │   "intent": "rag",                           │    │
│  │   "confidence": 0.95,                        │    │
│  │   "reason": "询问合同条款",                   │    │
│  │   "search_query": "XX合同的有效期条款",        │    │
│  │   "directive": "MUST_CALL_KNOWLEDGE_SEARCH", │    │
│  │   "override_prompt": "⚠️ [SYSTEM OVERRIDE]…" │    │
│  │ }                                            │    │
│  └──────────────────────────────────────────────┘    │
└─────────────────────────┬───────────────────────────┘
                          │
                    ┌─────┴─────┐
                    │           │
                    ▼           ▼
              ┌──────────┐ ┌──────────┐
              │  RAG 路径 │ │ 闲聊路径 │
              │ 必须检索  │ │ 跳过工具  │
              │ 注入指令  │ │ 直接作答  │
              └─────┬────┘ └─────┬────┘
                    │            │
                    ▼            ▼
              ┌──────────────────────────┐
              │  ChatService 统一路由     │
              │  Agent 模式 / RAG 模式    │
              └──────────────────────────┘
```

### 2.2 数据流

```
Phase 1: 意图识别
  Input:  query + 最近 2 轮历史
  LLM:   轻量模型（qwen-turbo）
  Output: IntentResult JSON

Phase 2: 路由决策
  - intent ∈ ["chitchat", "capability"] → 跳过检索，轻量 LLM 直接作答
  - intent ∈ ["rag", "document", "coreference", "ambiguous"] → 走正常 Agent/RAG 流程
  - confidence < 阈值 → 兜底走 RAG（安全策略）

Phase 3: 指令注入
  - override_prompt 注入到 Agent 的 agent_input 最前面
  - search_query 作为 knowledge_search 的预填参数
  - 主 LLM 收到强约束，优先执行工具调用
```

---

## 3. 意图类别定义

| 意图 | 标识 | 行为 | 示例 |
|------|------|------|------|
| 知识检索 | `rag` | 必须调用 `knowledge_search` | "这个合同的有效期多久" |
| 文档操作 | `document` | 必须调用 `knowledge_search` | "帮我总结财报"、"提取关键数据" |
| 指代消解 | `coreference` | 必须调用 `knowledge_search` | "它和前面那个有什么区别" |
| 闲聊问候 | `chitchat` | 跳过检索，直接作答 | "你好"、"谢谢"、"再见" |
| 能力问询 | `capability` | 跳过检索，直接作答 | "你能做什么"、"支持什么格式" |
| 模糊意图 | `ambiguous` | 默认走检索（安全兜底） | 单字、乱码、纯符号 |

---

## 4. 意图 LLM 设计

### 4.1 是否需要对话历史？

**需要，但只需最近 1-2 轮。**

| 场景 | 不需要历史 | 需要历史 |
|------|-----------|----------|
| “这个合同的有效期多久” | ❌ 无法判断“这个”指什么 | ✅ 历史中有“XX合同” |
| “帮我总结财报” | ✅ 明显是文档操作 | — |
| “你好” | ✅ 明显是闲聊 | — |
| “它和前面那个有什么区别” | ❌ 完全无法解析 | ✅ 必须消解指代 |
| “再详细一点” | ❌ 不知道什么内容 | ✅ 需要上一轮上下文 |

**历史传入策略**：
- 首轮对话（无历史）：不传历史，零额外成本
- 多轮对话：只传最近 **2 轮**（user + assistant 各 2 条）
- 单轮历史超过 2000 字符：截断到最近 2000 字符

### 4.2 返回什么信息？

**核心原则：不只是标签，而是"可执行的指令"。**

```json
{
  "intent": "rag",
  "confidence": 0.95,
  "reason": "用户询问合同条款内容",
  "search_query": "XX产品采购合同的有效期条款",
  "directive": "MUST_CALL_KNOWLEDGE_SEARCH",
  "override_prompt": "⚠️ [SYSTEM OVERRIDE] 本轮必须执行以下操作：\n1. 调用 knowledge_search 工具，检索查询：「XX产品采购合同的有效期条款」\n2. 基于检索结果作答，禁止使用对话历史中的内容\n3. 检索无结果时调用 web_search 补充"
}
```

### 4.3 字段说明

| 字段 | 类型 | 用途 | 消费方 |
|------|------|------|--------|
| `intent` | `str` | 路由决策（走 RAG 还是跳过） | ChatService 路由逻辑 |
| `confidence` | `float` | 低于阈值时兜底走 RAG | ChatService 兜底逻辑 |
| `reason` | `str` | 日志可观测性 | 日志系统 |
| `search_query` | `str` | 消解后的干净检索查询 | 注入给主 LLM 直接使用 |
| `directive` | `str` | 枚举值，代码层条件判断 | ChatService 条件分支 |
| `override_prompt` | `str` | 注入 Agent 上下文的硬约束 | 主 LLM 的 input 前缀 |

### 4.4 `override_prompt` 模板

**需要检索时**：
```
⚠️ [SYSTEM OVERRIDE] 本轮必须执行以下操作：
1. 调用 knowledge_search 工具，检索查询：「{search_query}」
2. 基于检索结果作答，禁止使用对话历史中的内容
3. 检索无结果时调用 web_search 补充
```

**不需要检索时**：
```
⚠️ [SYSTEM OVERRIDE] 本轮为闲聊/能力问询，直接回答即可。
禁止调用 knowledge_search、web_search 等任何工具。
```

### 4.5 为什么这个设计有效？

| 传统做法（不可靠） | 新做法（可靠） |
|-------------------|--------------|
| 意图 LLM 返回标签 → 主 LLM 可能忽略 | 意图 LLM 返回结构化指令 → 注入到 input 最前面 |
| 提示词是"建议" | `[SYSTEM OVERRIDE]` 是硬约束 |
| 主 LLM 自己处理指代消解 | 意图 LLM 提供现成的 `search_query` |
| 主 LLM 自行决定是否调用工具 | 指令明确告知"必须调用"或"禁止调用" |

---

## 5. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `app/utils/intent_classifier.py` | **新建** | 意图分类器核心模块 |
| `app/config/prompts/intent.txt` | **新建** | 意图分类专用提示词 |
| `app/config/chroma.yaml` | **修改** | 新增意图识别配置段 |
| `app/config/prompt.yaml` | **修改** | 注册 intent 模板 |
| `app/router/chat_service.py` | **修改** | 在 `handle_chat` 中插入意图识别 |
| `app/agent/agent_service.py` | **修改** | 接收 intent 并注入到 Agent 输入 |
| `app/utils/factory.py` | **修改** | 新增轻量级 intent 模型工厂方法 |

---

## 6. 详细设计

### 6.1 `app/config/prompts/intent.txt` — 意图分类提示词

```
你是意图分类器。分析用户输入和对话历史，输出 JSON。

## 分类规则
- "rag": 涉及私有文档内容的事实查询、概念解释、数据提取
- "document": 对文档进行查找、总结、提取、对比、改写等操作
- "coreference": 包含指代词（它/这/那/上面/前面/刚才），需结合历史消解
- "chitchat": 问候、感谢、告别等社交用语
- "capability": 询问系统能力、功能范围
- "ambiguous": 单字、乱码、纯符号，或无法判定意图

## 输入格式
历史对话（最近 2 轮，可能为空）:
{history}

用户最新消息:
{query}

## 输出格式（仅 JSON，无其他文字）
{
  "intent": "rag",
  "confidence": 0.95,
  "reason": "询问合同条款",
  "search_query": "消解指代后的完整检索查询",
  "directive": "MUST_CALL_KNOWLEDGE_SEARCH",
  "override_prompt": "⚠️ [SYSTEM OVERRIDE] 本轮必须执行以下操作：\n1. 调用 knowledge_search 工具，检索查询：「这里填 search_query」\n2. 基于检索结果作答，禁止使用对话历史中的内容\n3. 检索无结果时调用 web_search 补充"
}

## 重要规则
1. search_query 必须完成指代消解，不能包含"它"、"这个"等模糊指代
2. chitchat/capability 的 directive 必须是 "SKIP_ALL_TOOLS"
3. rag/document/coreference 的 directive 必须是 "MUST_CALL_KNOWLEDGE_SEARCH"
4. ambiguous 的 directive 必须是 "MUST_CALL_KNOWLEDGE_SEARCH"（安全兜底）
5. confidence 范围 0.0-1.0，chitchat 通常 > 0.9，模糊意图通常 < 0.7
```

### 6.2 `app/utils/intent_classifier.py` — 核心模块

**设计要求**：
- 单例模式，全局复用
- 使用轻量模型（`qwen3-turbo`），独立于主 LLM
- 同步调用，`classify()` 返回 `IntentResult` 对象
- 缓存机制：相同 `(query_hash, session_id)` 在 TTL 内复用
- 降级策略：分类失败 → 默认走 RAG 检索（安全优先）
- 超时控制：`intent_timeout` 秒内无响应 → 降级

**核心接口**：
```python
class IntentResult:
    intent: str          # rag / document / coreference / chitchat / capability / ambiguous
    confidence: float    # 0.0 - 1.0
    reason: str          # 分类理由
    search_query: str    # 消解后的检索查询
    directive: str       # MUST_CALL_KNOWLEDGE_SEARCH / SKIP_ALL_TOOLS
    override_prompt: str # 注入主 LLM 的硬约束指令

class IntentClassifier:
    def classify(self, query: str, session_id: str, 
                 history: list | None = None) -> IntentResult:
        """同步分类，返回结构化结果。"""
        ...

    def is_rag_intent(self, result: IntentResult) -> bool:
        """判断是否需要走 RAG 检索。"""
        ...

    def get_override_prompt(self, result: IntentResult) -> str:
        """获取注入主 LLM 的 override_prompt。"""
        ...
```

### 6.3 `app/config/chroma.yaml` 新增配置

```yaml
# --- 意图识别 ---
intent_enabled: true                       # 总开关，false 时完全跳过意图识别
intent_model: "qwen3-turbo"               # 轻量模型（优先用便宜的，降低运营成本）
intent_timeout: 5                          # 超时秒数，超时后降级为默认走 RAG
intent_cache_ttl: 300                      # 缓存有效期（秒），相同 query+session 复用
intent_confidence_threshold: 0.7           # 置信度阈值，低于此值默认走 RAG（安全兜底）
intent_skip_intents: ["chitchat", "capability"]  # 跳过检索的意图类型
intent_history_turns: 2                    # 传入意图分类器的历史轮次
intent_max_history_chars: 2000             # 传入意图分类器的历史最大字符数
```

### 6.4 `app/config/prompt.yaml` 修改

```yaml
templates:
  hyde: app/config/prompts/hyde.txt
  agent: app/config/prompts/agent.txt
  summary: app/config/prompts/summary.txt
  vision: app/config/prompts/vision.txt
  rag_answer: app/config/prompts/rag_answer.txt
  intent: app/config/prompts/intent.txt    # 新增
```

### 6.5 `app/utils/factory.py` 新增方法

```python
def create_intent_model():
    """创建意图分类专用轻量模型。

    独立于主 LLM，使用便宜的小模型降低运营成本。
    通过 INTENT_MODEL_TYPE 环境变量切换。
    """
    intent_type = os.getenv("INTENT_MODEL_TYPE", "QWEN_TURBO").upper()

    if intent_type == "QWEN_TURBO":
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(
            model_name=os.getenv("INTENT_MODEL_NAME", "qwen3-turbo"),
            dashscope_api_key=get_api_key(),
            temperature=0.0,  # 分类任务不需要随机性
        )
    elif intent_type == "DEEPSEEK_LITE":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.getenv("INTENT_MODEL_NAME", "deepseek-chat"),
            openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
            openai_api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            temperature=0.0,
        )
    else:
        raise ValueError(f"不支持的 INTENT_MODEL_TYPE: {intent_type}")
```

### 6.6 `app/router/chat_service.py` 修改

**修改 `handle_chat()` 方法**，在步骤 1（会话管理）之后、步骤 2（路由分发）之前插入意图识别：

```python
async def handle_chat(self, query: str, session_id: str | None,
                      user_id: str, mode: str = "agent") -> AsyncIterator[str]:
    # 1. 会话管理（不变）
    if not session_id:
        session_id = self._memory.create_conversation(user_id, query[:_TITLE_TRUNCATE])
        yield f"data: {json.dumps({'event': 'session_created', 'session_id': session_id})}\n\n"

    # 1.5 ★ 意图识别（新增）
    intent_result = None
    if get_config("intent_enabled", True):
        try:
            from app.utils.intent_classifier import get_intent_classifier
            classifier = get_intent_classifier()
            history = self._memory.load_context(session_id) if session_id else None
            intent_result = classifier.classify(query, session_id or "", history)
            logger.info(
                f"【意图识别】intent={intent_result.intent}, "
                f"confidence={intent_result.confidence:.2f}, "
                f"reason={intent_result.reason}"
            )
            # 将 intent 信息作为 SSE 事件推送给前端（可选，用于调试）
            yield f"data: {json.dumps({'event': 'intent', 'data': {
                'intent': intent_result.intent,
                'confidence': intent_result.confidence
            }})}\n\n"
        except Exception as e:
            logger.warning(f"【意图识别】分类失败，降级走 RAG: {e}")
            intent_result = None  # 降级：默认走 RAG

    # 2. 路由分发
    if intent_result and intent_result.intent in get_config("intent_skip_intents", []):
        # 闲聊/能力问询 → 跳过检索，直接作答
        logger.info(f"【对话】路由 → 意图直答: intent={intent_result.intent}")
        async for sse in self._handle_direct_answer(query, session_id, user_id, intent_result):
            yield sse
    elif mode == "rag":
        logger.info(f"【对话】路由 → RAG直通")
        async for sse in self._handle_rag_stream(query, user_id, session_id):
            yield sse
    else:
        logger.info(f"【对话】路由 → Agent工具链")
        async for sse in self._handle_agent_stream(
            query, session_id, user_id, intent_result
        ):
            yield sse
```

### 6.7 `app/agent/agent_service.py` 修改

**修改 `stream_chat()` 方法**，接收 `intent_result` 参数并注入 override_prompt：

```python
async def stream_chat(self, query: str, session_id: str,
                      user_id: str = "default_user",
                      intent_result: IntentResult | None = None) -> AsyncIterator[dict]:
    """流式执行 Agent 对话。

    Args:
        intent_result: 意图分类结果，非 None 时注入 override_prompt 到 Agent 输入
    """
    chat_history = memory_svc.load_context(session_id)

    # 构建 agent_input（注入 intent override）
    agent_input = query
    if intent_result and intent_result.override_prompt:
        # 意图识别结果 → 注入硬约束指令
        agent_input = intent_result.override_prompt + "\n\n" + query
        logger.info(
            f"【Agent】注入意图指令: directive={intent_result.directive}, "
            f"search_query={intent_result.search_query}"
        )
    elif chat_history:
        # 无意图识别结果 → 使用原有逻辑（模糊提醒）
        agent_input = (
            "[⚠️ 本轮必须调用 knowledge_search 检索知识库，"
            "禁止基于历史对话中的内容直接作答]\n\n" + query
        )
    # ... 其余逻辑不变
```

### 6.8 `_handle_direct_answer()` 新增方法

闲聊/能力问询场景下，**不注册 Agent 工具**，用主 LLM 直接作答：

```python
async def _handle_direct_answer(self, query: str, session_id: str,
                                 user_id: str, intent_result) -> AsyncIterator[str]:
    """意图为闲聊/能力问询时，直接作答，不调用任何工具。"""
    from app.core.background_init import init_manager
    
    llm = init_manager.chat_model or create_chat_model()
    history = self._memory.load_context(session_id)
    
    # 简单 prompt 直接作答
    messages = [
        {"role": "system", "content": "你是知识库助手，用户正在闲聊或询问能力。简短友好地回答，不超过 100 字。"},
        *[{"role": m["role"], "content": m["content"]} for m in (history or [])],
        {"role": "user", "content": query},
    ]
    
    answer = ""
    for chunk in llm.stream(messages):
        if chunk.content:
            answer += chunk.content
            yield f"data: {json.dumps({'event': 'token', 'data': chunk.content})}\n\n"
    
    yield f"data: {json.dumps({'event': 'done', 'data': ''})}\n\n"
    
    self._memory.append_messages(session_id, query, answer)
```

---

## 7. 实施阶段

| 阶段 | 内容 | 输出 | 工作 | 风险 |
|------|------|------|------|------|
| **阶段1** | 基础设施 | `intent.txt` + `intent_classifier.py` + `factory.py` 修改 + `chroma.yaml` 配置 | 1.5h | 低 |
| **阶段2** | 流程集成 | 修改 `chat_service.py` 插入意图识别 + 新增 `_handle_direct_answer` | 1h | 中 |
| **阶段3** | Agent 注入 | 修改 `agent_service.py` 接收 intent 并注入 override_prompt | 0.5h | 中 |
| **阶段4** | 测试验证 | 端到端测试 + 日志监控 + 边界场景 | 1h | 低 |
| **总计** | | | **4h** | |

### 7.1 阶段1 详细步骤

1. 创建 `app/config/prompts/intent.txt`
2. 创建 `app/utils/intent_classifier.py`
3. 修改 `app/config/chroma.yaml`，新增 `intent_*` 配置项
4. 修改 `app/config/prompt.yaml`，注册 `intent` 模板
5. 修改 `app/utils/factory.py`，新增 `create_intent_model()`

### 7.2 阶段2 详细步骤

1. 修改 `app/router/chat_service.py`：
   - `handle_chat()` 中插入意图识别逻辑
   - 新增 `_handle_direct_answer()` 方法
   - 修改 `_handle_agent_stream()` 调用，传入 `intent_result`

### 7.3 阶段3 详细步骤

1. 修改 `app/agent/agent_service.py`：
   - `stream_chat()` 新增 `intent_result` 参数
   - 修改 `agent_input` 构建逻辑，注入 `override_prompt`

### 7.4 阶段4 测试用例

| 测试场景 | 输入 | 预期意图 | 预期行为 |
|----------|------|---------|---------|
| 文档查询 | "这个合同的有效期多久" | `rag` | 调用 knowledge_search |
| 文档操作 | "帮我总结财报" | `document` | 调用 knowledge_search |
| 指代消解 | "它和前面那个有什么区别" | `coreference` | 调用 knowledge_search |
| 闲聊问候 | "你好" | `chitchat` | 直接作答，不调用工具 |
| 能力问询 | "你能做什么" | `capability` | 直接作答，不调用工具 |
| 模糊输入 | "嗯" | `ambiguous` | 兜底调用 knowledge_search |
| 多轮指代 | 第1轮"XX合同" → 第2轮"它" | `coreference` | search_query 已消解 |

---

## 8. 风险控制

| 风险 | 概率 | 影响 | 措施 |
|------|------|------|------|
| 意图分类错误 | 中 | 中 | `confidence < 0.7` 兜底走 RAG；失败默认走 RAG |
| 引入额外延迟 | 中 | 低 | 轻量模型 < 2s；缓存 5 分钟；超时 5s 降级 |
| API 成本增加 | 低 | 低 | `qwen3-turbo` 比主模型便宜 10x；闲聊场景跳过 Agent 反而省钱 |
| 破坏现有流程 | 低 | 高 | `intent_enabled: false` 一键关闭，完全回退 |
| 缓存命中旧结果 | 低 | 低 | TTL 5 分钟；session_id 隔离；query_hash 精确匹配 |

### 8.1 降级矩阵

| 场景 | 降级策略 |
|------|---------|
| 意图 LLM 超时（>5s） | 默认走 RAG 检索 |
| 意图 LLM 返回非 JSON | 默认走 RAG 检索 |
| 意图 LLM 初始化失败 | 跳过意图识别，走原有流程 |
| `intent_enabled: false` | 完全跳过意图识别，走原有流程 |

---

## 9. 日志规范

```log
[意图识别] session=abc123, intent=rag, confidence=0.95, reason=询问合同条款, latency=1.2s
[意图识别] session=def456, intent=chitchat, confidence=0.98, reason=问候语, latency=0.8s
[意图识别] session=ghi789, intent=coreference, confidence=0.72, search_query=XX产品采购合同的有效期, latency=1.5s
[意图识别] 分类失败，降级走 RAG: TimeoutError
[Agent] 注入意图指令: directive=MUST_CALL_KNOWLEDGE_SEARCH, search_query=XX合同的有效期条款
[对话] 路由 → 意图直答: intent=chitchat
```

---

## 10. 接口定义

### 10.1 IntentResult

```python
@dataclass
class IntentResult:
    intent: str          # rag / document / coreference / chitchat / capability / ambiguous
    confidence: float    # 0.0 - 1.0
    reason: str          # 分类理由
    search_query: str    # 消解后的检索查询（chitchat/capability 时为空）
    directive: str       # MUST_CALL_KNOWLEDGE_SEARCH / SKIP_ALL_TOOLS
    override_prompt: str # 注入主 LLM 的硬约束指令

    def is_rag_intent(self) -> bool:
        """是否需要走 RAG 检索"""
        return self.intent in ("rag", "document", "coreference", "ambiguous")
```

### 10.2 IntentClassifier

```python
class IntentClassifier:
    def __init__(self):
        self._model = None
        self._cache: dict[str, tuple[float, IntentResult]] = {}

    def classify(self, query: str, session_id: str,
                 history: list | None = None) -> IntentResult:
        """同步分类，返回结构化结果。

        1. 检查缓存
        2. 构建 prompt（含最近 2 轮历史）
        3. 调用轻量 LLM
        4. 解析 JSON 返回
        5. 失败时返回默认 IntentResult（走 RAG 兜底）
        """
        ...
```

---

## 11. 成本估算

| 场景 | 传统方案 | 新方案 | 变化 |
|------|---------|--------|------|
| 文档查询 | 主 LLM 完整推理 ≈ 5000 token | 意图 LLM 100 token + 主 LLM 5000 token | +2% |
| 闲聊/能力问询 | 主 LLM Agent 推理 ≈ 5000 token | 意图 LLM 100 token + 主 LLM 直接作答 500 token | **-88%** |
| 缓存命中 | 主 LLM 完整推理 | 缓存命中 0 token + 主 LLM 正常推理 | 0% |

**结论**：闲聊场景大幅节省，文档查询微增，整体成本下降。

---

## 12. 依赖

| 依赖 | 说明 |
|------|------|
| `langchain-community` | 已有，用于 `ChatTongyi` |
| `langchain-core` | 已有 |
| `langchain-openai` | 已有，用于 DeepSeek 模型 |
| 阿里云百炼 API Key | 已有 |
| 无新增第三方库 | — |

---

## 附录 A：agent.txt 简化方案

引入意图识别后，[agent.txt](file:///D:/Knowledge_rag_system/app/config/prompts/agent.txt) 中的"意图分类"章节可以简化或移除，因为意图判定已由前置模块完成：

```diff
- ## 2. 意图分类（最高优先级，收到消息第一步）
- 收到用户消息后，**先分类，再行动**。无法明确判定时，一律执行强制检索。
- 
- ### 2.1 强制检索 → 调用 `knowledge_search`
- ...
- ### 2.2 免检索直答 → 禁止调用任何工具
- ...

+ ## 2. 工具调用规则
+ 收到 [SYSTEM OVERRIDE] 指令时，严格按照指令执行：
+ - MUST_CALL_KNOWLEDGE_SEARCH → 必须调用 knowledge_search
+ - SKIP_ALL_TOOLS → 禁止调用任何工具，直接作答
```

---

## 附录 B：评估指标

| 指标 | 目标值 | 测量方式 |
|------|--------|---------|
| 意图分类准确率 | > 90% | 人工抽样 100 条标注 |
| 意图分类延迟 | < 2s (P95) | 日志统计 |
| 缓存命中率 | > 30% | 日志统计 |
| knowledge_search 跳过率 | < 5% | 日志统计 |
| 闲聊场景 token 节省 | > 80% | API 调用统计 |