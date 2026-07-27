# 意图识别模块实施计划 v2

**版本**: v2.0  
**日期**: 2026-07-27  
**状态**: 待审核  
**基于**: v1.0 plan + 实际项目代码审查 + 需求讨论结论

---

## 需求对齐总结

| 决策项 | 结论 |
|--------|------|
| 分类粒度 | **二分类**：`NEED_RAG` / `NO_RAG` |
| 作用范围 | 仅 Agent 模式，RAG 直通模式不参与 |
| 识别模型 | **DeepSeek 小版本**（`deepseek-chat`），通过环境变量配置 |
| 意图注入方式 | **注入 Agent System Prompt**（动态拼接） |
| 对话历史 | **需要**，传入最近 2 轮摘要，不为空时帮助指代消解 |

---

## 1. 现状分析（来自实际代码审查）

### 1.1 当前请求链路

```
handle_chat() [chat_service.py:25]
  ├── 会话管理 (session_id 创建/验证)
  ├── 路由分发:
  │   ├── mode="rag"  → _handle_rag_stream()    → RAGService.search()
  │   └── mode="agent" → _handle_agent_stream() → AgentService.stream_chat()
  │                                                  ├── 加载 history (最多5轮)
  │                                                  ├── 多轮时注入检索提醒前缀
  │                                                  ├── 创建 AgentExecutor (含 tools)
  │                                                  └── astream_events() 流式输出
```

### 1.2 关键观察

1. **Agent prompt 已有意图分类章节**（`agent.txt` 第2节），但由主 LLM 自行判断，约束力不足
2. **多轮对话已有 recency bias 注入**（`agent_service.py:232-235`）：`[⚠️ 本轮必须调用 knowledge_search...]` 前缀强制触发 tool call
3. **模型创建在 `factory.py`**，通过 `LLM_TYPE` 环境变量切换，ChatOpenAI/DashScope 兼容端点
4. **Prompt 模板在 `prompt.yaml` + `prompts/*.txt`**，通过 `PromptLoader` 加载
5. **项目配置用 `.env`**，`get_config()` 兼容 `.env` 变量和 yaml 配置

### 1.3 可复用的现有机制

| 现有机制 | 位置 | 如何复用 |
|----------|------|---------|
| `PromptLoader` | `app/utils/prompt_loader.py` | 加载意图分类 prompt（新增 `intent_classifier` 模板） |
| `create_chat_model()` | `app/utils/factory.py` | 参考其模式，新增 `create_intent_model()` |
| `ConversationMemoryService.load_context()` | `app/memory/memory_service.py` | 为意图分类器加载最近 2 轮 history |
| 多轮注入前缀模式 | `agent_service.py:232-235` | 意图注入取代/增强当前的前缀注入 |

---

## 2. 架构设计

### 2.1 修改后链路

```
handle_chat() [chat_service.py]
  ├── 1. 会话管理 (不变)
  ├── ★ 2. 意图识别 (新增) ──────────────────────┐
  │     ├── 仅 mode="agent" 时触发               │
  │     ├── IntentClassifier.classify()           │
  │     │   ├── 加载最近2轮 history                │
  │     │   ├── 调用 DeepSeek 小版 (temperature=0) │
  │     │   └── 返回 IntentResult (NEED_RAG/NO_RAG)│
  │     └── 失败/超时 → 降级为 NEED_RAG (安全兜底) │
  ├── 3. 路由分发                                 │
  │   ├── mode="rag"    → _handle_rag_stream()    │
  │   └── mode="agent"  → _handle_agent_stream(intent_result) ◄─┘
  │                           │
  │                           ▼
  │                    AgentService.stream_chat(intent_result)
  │                      ├── 动态拼接 system_prompt:
  │                      │    NEED_RAG → [SYSTEM OVERRIDE] 必须调用 knowledge_search
  │                      │    NO_RAG   → [SYSTEM OVERRIDE] 跳过知识库检索
  │                      ├── 创建 AgentExecutor
  │                      └── astream_events() 流式输出
```

### 2.2 模块边界

```
app/intent/                          # ★ 新增目录
├── __init__.py                      # 导出 IntentResult, IntentClassifier
├── intent_classifier.py             # 核心分类器
└── intent_config.py                 # 配置常量与默认值

app/config/prompts/
└── intent_classifier.txt            # ★ 新增：意图分类 prompt 模板

app/config/
├── prompt.yaml                      # 修改：注册 intent_classifier 模板
└── chroma.yaml                      # 修改：新增意图识别配置段 (可选，也可用 .env)

app/utils/
└── factory.py                       # 修改：新增 create_intent_model()

app/router/
└── chat_service.py                  # 修改：插入意图识别 + 传入 intent_result

app/agent/
└── agent_service.py                 # 修改：接收 intent_result，动态拼接 prompt

app/config/prompts/
└── agent.txt                        # 修改：简化意图分类章节，适配 [SYSTEM OVERRIDE]
```

---

## 3. 两个核心设计问题

### 3.1 意图识别 LLM 需要对话 History 吗？

**需要，但策略是"摘要化 + 截断"。**

裸传全量 LangChain 消息对象太重，意图分类器不需要逐字对话内容，只需要上下文骨架：

```python
def _build_history_context(self, history: list) -> str:
    """将 LangChain 消息列表转为意图分类用的精简上下文。"""
    if not history:
        return "（无历史对话，这是首轮对话）"

    # 只取最近 2 轮（user + assistant 各 2 = 4 条消息）
    recent = history[-4:]
    lines = []
    for msg in recent:
        role = "用户" if msg.__class__.__name__ == "HumanMessage" else "助手"
        # 截断每条消息到 200 字符
        content = msg.content[:200] if hasattr(msg, "content") else str(msg)[:200]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
```

**为什么这样设计：**
- 意图分类只需判断"用户当前在问什么"，不需要完整答案内容
- 200 字符足够理解话题延续性（如"它"指什么）
- 减少 token 开销（意图 LLM 每次只需 ~150 token 的 history）

### 3.2 返回什么信息让主 LLM 稳定调用 RAG tool？

**返回结构化 IntentResult，关键是 `action_directive` 字段的系统级注入。**

核心思路：不依赖主 LLM "理解"意图标签，而是把意图转化为**它无法忽略的 system prompt 前缀**：

```python
@dataclass
class IntentResult:
    intent: str              # "NEED_RAG" | "NO_RAG"
    confidence: float        # 0.0 - 1.0
    reason: str              # 分类理由（日志用）
    search_query: str        # 指代消解后的检索词（NEED_RAG 时有值）
    action_directive: str    # 注入 Agent system prompt 的硬约束指令块
```

**`action_directive` 的两种形态：**

当 `NEED_RAG` 时：
```
[SYSTEM OVERRIDE — 本次对话硬约束]
意图判定: 需要检索知识库
原因: 用户询问文档中的合同条款
预检索查询: XX产品采购合同的有效期条款
执行指令: 必须首先调用 knowledge_search 工具，禁止跳过检索直接作答。
          如果知识库无结果，可以调用 web_search 补充。
```

当 `NO_RAG` 时：
```
[SYSTEM OVERRIDE — 本次对话硬约束]
意图判定: 无需检索知识库
原因: 用户在进行社交问候
执行指令: 跳过 knowledge_search，根据当前 query 直接作答或调用其他工具。
```

**为什么比现有方案更可靠：**

| 维度 | 现有多轮注入 | 意图识别注入 |
|------|-------------|-------------|
| 信息来源 | 仅判断"有历史"就注入 | 独立 LLM 分析语义后注入 |
| 措辞 | 统一的模糊警告 | 区分 NEED_RAG/NO_RAG 的精确指令 |
| NO_RAG 处理 | 不存在（所有多轮都强制检索） | 明确告知"跳过知识库检索" |
| 覆盖范围 | 仅多轮对话 | 首轮 + 多轮全覆盖 |

---

## 4. 详细实施步骤

### 阶段 1：基础设施（3 个新文件 + 2 个配置修改）

#### Step 1.1: 新建 `app/config/prompts/intent_classifier.txt`

```
你是意图分类器。分析用户输入，判断是否需要在知识库中检索。输出 JSON。

## 分类规则
- NEED_RAG: 询问文档内容、专有名词、需要知识库才能回答的问题
- NO_RAG: 闲聊问候、感谢告别、询问系统能力、无意义输入

## 输入
对话历史:
{history}

用户消息:
{query}

## 输出（仅 JSON，无其他文字）
{{
  "intent": "NEED_RAG",
  "confidence": 0.95,
  "reason": "用户询问合同条款",
  "search_query": "消解指代后的检索查询"
}}

## 规则
1. search_query 不能包含"它"、"这个"等未消解的指代词，需结合历史替换为具体实体
2. confidence 0.0-1.0，闲聊通常 > 0.9，含指代词的查询应降低 confidence
3. 无法判定时 intent="NEED_RAG"（安全兜底）
```

#### Step 1.2: 新建 `app/intent/__init__.py`

```python
"""意图识别模块 — 前置意图分类器。"""
from app.intent.intent_classifier import IntentClassifier, IntentResult

__all__ = ["IntentClassifier", "IntentResult"]
```

#### Step 1.3: 新建 `app/intent/intent_config.py`

```python
"""意图识别模块配置常量与默认值。"""
from app.config.loader import get_config

# 模型配置
INTENT_MODEL = get_config("intent_model", "deepseek-chat")
INTENT_MODEL_TYPE = get_config("INTENT_MODEL_TYPE", "DEEPSEEK")

# 超时与降级
INTENT_TIMEOUT = int(get_config("intent_timeout", 5))
INTENT_CACHE_TTL = int(get_config("intent_cache_ttl", 300))
INTENT_CONFIDENCE_THRESHOLD = float(get_config("intent_confidence_threshold", 0.7))

# 历史配置
INTENT_HISTORY_TURNS = int(get_config("intent_history_turns", 2))
INTENT_MAX_HISTORY_CHARS = int(get_config("intent_max_history_chars", 500))
INTENT_MAX_QUERY_CHARS = int(get_config("intent_max_query_chars", 500))

# 总开关
INTENT_ENABLED = get_config("intent_enabled", True)
```

#### Step 1.4: 新建 `app/intent/intent_classifier.py`

核心设计要点：
- 单例（通过 `get_intent_classifier()` 获取）
- 同步调用（`classify()` 返回 `IntentResult`）
- 内存缓存（同一 session + query_hash 5分钟复用）
- 降级策略（任何异常 → 返回 NEED_RAG 兜底）
- 独立轻量模型（temperature=0）

```python
"""意图分类器 — 独立轻量 LLM 完成意图前置判定。"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)

# 单例
_classifier: Optional["IntentClassifier"] = None


@dataclass
class IntentResult:
    """意图分类结构化结果。"""
    intent: str = "NEED_RAG"           # NEED_RAG | NO_RAG
    confidence: float = 0.5
    reason: str = ""
    search_query: str = ""
    action_directive: str = ""

    def is_rag_needed(self) -> bool:
        return self.intent == "NEED_RAG"


class IntentClassifier:
    """意图分类器：调用独立轻量 LLM，前置判断 query 是否需要检索。

    使用 DeepSeek 小版本模型，temperature=0，同步调用。
    """

    def __init__(self):
        self._model = None
        self._cache: dict[str, tuple[float, "IntentResult"]] = {}
        self._prompt_loader = PromptLoader()

    # ── 公开接口 ──────────────────────────────────────────

    def classify(
        self, query: str, session_id: str,
        history: list | None = None,
    ) -> IntentResult:
        """同步分类，返回结构化结果。异常时降级返回 NEED_RAG。"""
        t_start = time.time()

        # 1. 总开关关闭 → 直接返回 NEED_RAG（跳过识别，走原有流程）
        if not get_config("intent_enabled", True):
            return IntentResult(
                intent="NEED_RAG", confidence=1.0,
                reason="意图识别已关闭", action_directive="",
            )

        # 2. 检查缓存
        cache_key = self._cache_key(query, session_id)
        if cache_key in self._cache:
            cached_at, result = self._cache[cache_key]
            if time.time() - cached_at < int(get_config("intent_cache_ttl", 300)):
                logger.debug(f"【意图识别】缓存命中: intent={result.intent}")
                return result

        # 3. 调用轻量 LLM
        try:
            result = self._do_classify(query, history)
            if result is None:
                result = self._fallback_result("LLM 返回空")
        except Exception as e:
            logger.warning(f"【意图识别】分类异常: {e}")
            result = self._fallback_result(str(e))

        # 4. 低置信度 → 兜底 NEED_RAG
        threshold = float(get_config("intent_confidence_threshold", 0.7))
        if result.confidence < threshold:
            logger.info(
                f"【意图识别】置信度低于阈值 ({result.confidence:.2f} < {threshold})，"
                f"降级 NEED_RAG"
            )
            result = self._fallback_result(
                f"低置信度({result.confidence:.2f})",
            )

        # 5. 缓存
        self._cache[cache_key] = (time.time(), result)

        elapsed = time.time() - t_start
        logger.info(
            f"【意图识别】session={session_id[:8]}..., intent={result.intent}, "
            f"confidence={result.confidence:.2f}, reason={result.reason}, "
            f"latency={elapsed:.2f}s"
        )
        return result

    # ── 内部方法 ──────────────────────────────────────────

    def _get_model(self):
        if self._model is None:
            from app.utils.factory import create_intent_model
            self._model = create_intent_model()
        return self._model

    def _do_classify(self, query: str, history: list | None) -> Optional[IntentResult]:
        """执行 LLM 调用，解析 JSON 返回。"""
        model = self._get_model()
        history_text = self._build_history_context(history)
        max_chars = int(get_config("intent_max_query_chars", 500))
        query_truncated = query[:max_chars]

        prompt = self._prompt_loader.load(
            "intent_classifier",
            history=history_text,
            query=query_truncated,
        )
        if not prompt:
            logger.warning("【意图识别】prompt 模板为空，降级")
            return None

        response = model.invoke(prompt)
        raw = response.content if hasattr(response, "content") else str(response)
        return self._parse_response(raw, query)

    def _parse_response(self, raw: str, query: str) -> Optional[IntentResult]:
        """解析 LLM 返回的 JSON，构建 IntentResult。"""
        # 提取 JSON（LLM 可能会包裹在 ```json 代码块中）
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            logger.warning(f"【意图识别】无法从响应中提取 JSON: {raw[:200]}")
            return None

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            logger.warning(f"【意图识别】JSON 解析失败: {e}, raw={raw[:200]}")
            return None

        intent = data.get("intent", "NEED_RAG")
        confidence = float(data.get("confidence", 0.5))
        reason = data.get("reason", "")
        search_query = data.get("search_query", query)

        # 规范化 intent 值
        if intent not in ("NEED_RAG", "NO_RAG"):
            intent = "NEED_RAG"

        # 构建 action_directive
        action_directive = self._build_directive(intent, reason, search_query)

        return IntentResult(
            intent=intent,
            confidence=confidence,
            reason=reason,
            search_query=search_query,
            action_directive=action_directive,
        )

    def _build_directive(
        self, intent: str, reason: str, search_query: str,
    ) -> str:
        """构建注入 Agent system prompt 的硬约束指令块。"""
        if intent == "NEED_RAG":
            return (
                "[SYSTEM OVERRIDE — 本次对话硬约束]\n"
                f"意图判定: 需要检索知识库\n"
                f"原因: {reason}\n"
                f"预检索查询: {search_query}\n"
                "执行指令: 必须首先调用 knowledge_search 工具进行检索，"
                "禁止跳过检索直接作答。知识库无结果时可调用 web_search 补充。"
            )
        else:
            return (
                "[SYSTEM OVERRIDE — 本次对话硬约束]\n"
                f"意图判定: 无需检索知识库\n"
                f"原因: {reason}\n"
                "执行指令: 跳过 knowledge_search，根据当前 query "
                "直接作答或调用其他工具。"
            )

    def _build_history_context(self, history: list | None) -> str:
        """将 LangChain 消息列表转为意图分类用的精简上下文。"""
        if not history:
            return "（无历史对话，这是首轮对话）"

        max_chars = int(get_config("intent_max_history_chars", 500))
        turns = int(get_config("intent_history_turns", 2))
        recent = history[-(turns * 2):]  # 每轮 user+assistant 两条

        lines = []
        for msg in recent:
            role = "用户" if msg.__class__.__name__ == "HumanMessage" else "助手"
            content = ""
            if hasattr(msg, "content"):
                content = str(msg.content) if msg.content else ""
            else:
                content = str(msg)
            content = content[:max_chars]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _fallback_result(self, reason: str) -> IntentResult:
        """降级结果：默认 NEED_RAG，无 action_directive（走原有逻辑）。"""
        return IntentResult(
            intent="NEED_RAG",
            confidence=0.5,
            reason=f"降级: {reason}",
            search_query="",
            action_directive="",
        )

    @staticmethod
    def _cache_key(query: str, session_id: str) -> str:
        raw = f"{session_id}:{query.strip()}"
        return hashlib.md5(raw.encode()).hexdigest()


def get_intent_classifier() -> IntentClassifier:
    """获取 IntentClassifier 单例。"""
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier
```

#### Step 1.5: 修改 `app/utils/factory.py` — 新增 `create_intent_model()`

在文件末尾添加：

```python
# ============================================================
# Intent Classification Model（意图识别专用轻量模型）
# ============================================================

def create_intent_model():
    """创建意图识别专用轻量模型。

    独立于主 LLM，使用 DeepSeek 小版本降低延迟和成本。
    通过 INTENT_MODEL_TYPE 环境变量切换。
    """
    intent_type = os.getenv("INTENT_MODEL_TYPE", "DEEPSEEK").upper()

    from langchain_openai import ChatOpenAI

    # 意图分类使用 temperature=0，确保输出稳定
    _T = 0.0

    if intent_type == "DEEPSEEK":
        deepseek_key = _env("DEEPSEEK_API_KEY")
        if deepseek_key:
            primary = ChatOpenAI(
                model=_env("INTENT_MODEL_NAME", "deepseek-chat"),
                openai_api_key=deepseek_key,
                openai_api_base=_env(
                    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
                ),
                temperature=_T,
            )
        else:
            # 回退到阿里云 DashScope 兼容端点
            primary = ChatOpenAI(
                model=_env("INTENT_MODEL_NAME", "deepseek-v4-pro"),
                openai_api_key=get_api_key(),
                openai_api_base=_env(
                    "ALIYUN_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
                temperature=_T,
            )

        # 降级：阿里云通义千问
        from langchain_community.chat_models import ChatTongyi
        fallback = ChatTongyi(
            model_name=_env("INTENT_FALLBACK_MODEL", "qwen3-turbo"),
            dashscope_api_key=get_api_key(),
            temperature=_T,
        )
        return primary.with_fallbacks([fallback])

    elif intent_type == "QWEN":
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(
            model_name=_env("INTENT_MODEL_NAME", "qwen3-turbo"),
            dashscope_api_key=get_api_key(),
            temperature=_T,
        )

    else:
        raise ValueError(
            f"不支持的 INTENT_MODEL_TYPE: {intent_type}，可选值: DEEPSEEK / QWEN"
        )
```

#### Step 1.6: 修改 `app/config/prompt.yaml` — 注册新模板

```yaml
templates:
  hyde: app/config/prompts/hyde.txt
  agent: app/config/prompts/agent.txt
  summary: app/config/prompts/summary.txt
  vision: app/config/prompts/vision.txt
  rag_answer: app/config/prompts/rag_answer.txt
  intent_classifier: app/config/prompts/intent_classifier.txt    # 新增
```

#### Step 1.7: 修改 `.env` — 新增意图识别配置

```bash
# --- 意图识别 ---
INTENT_MODEL_TYPE=DEEPSEEK
INTENT_MODEL_NAME=deepseek-chat
INTENT_FALLBACK_MODEL=qwen3-turbo
INTENT_TIMEOUT=5
INTENT_CACHE_TTL=300
INTENT_CONFIDENCE_THRESHOLD=0.7
INTENT_HISTORY_TURNS=2
INTENT_MAX_HISTORY_CHARS=500
```

---

### 阶段 2：流程集成（1 个文件修改）

#### Step 2.1: 修改 `app/router/chat_service.py`

**修改位置：`handle_chat()` 方法，第 2 步（路由分发）之前插入意图识别。**

具体改动：

1. `handle_chat()` 中，`mode == "rag"` 分支不触发意图识别；`mode == "agent"` 分支先调用 IntentClassifier
2. 意图识别失败 → 降级，intent_result=None，走原有 Agent 流程
3. `_handle_agent_stream()` 调用时传入 `intent_result`
4. 首轮对话也需要意图识别（不只多轮）

关键代码变更（在 `handle_chat` 方法中）：

```python
# --- 修改前 ---
if mode == "rag":
    async for sse in self._handle_rag_stream(query, user_id, session_id):
        yield sse
else:
    async for sse in self._handle_agent_stream(query, session_id, user_id):
        yield sse

# --- 修改后 ---
if mode == "rag":
    # RAG 直通模式不触发意图识别
    async for sse in self._handle_rag_stream(query, user_id, session_id):
        yield sse
else:
    # ★ Agent 模式：先进行意图识别
    intent_result = None
    if get_config("intent_enabled", True):
        try:
            from app.intent import IntentClassifier, IntentResult
            classifier = IntentClassifier()
            history = self._memory.load_context(session_id)
            intent_result = classifier.classify(query, session_id, history)
        except Exception as e:
            logger.warning(f"【意图识别】分类失败，降级走默认 Agent 流程: {e}")
            intent_result = None

    async for sse in self._handle_agent_stream(
        query, session_id, user_id, intent_result
    ):
        yield sse
```

**注意：v1 plan 中的 `_handle_direct_answer()` 新增方法暂不引入。** NO_RAG 场景仍走 Agent 流程，但 system prompt 中会注入 "跳过 knowledge_search" 的硬约束，主 LLM 仍可根据 query 调用其他工具或直接作答：
- 改动范围更小，`chat_service.py` 不需要新增方法
- NO_RAG 场景依然有对话历史管理、持久化等完整能力
- 如果意图分类误判，主 LLM 仍有调用工具的能力（比完全跳过 Agent 更安全）

---

### 阶段 3：Agent 注入（1 个文件修改）

#### Step 3.1: 修改 `app/agent/agent_service.py`

**修改位置：`stream_chat()` 方法和 `_create_executor()` 方法。**

具体改动：

1. `stream_chat()` 签名新增 `intent_result: IntentResult | None = None` 参数
2. 有 `intent_result` 时，用 `action_directive` 替换当前的多轮注入前缀
3. 修改 agent_input 构建逻辑，优先级：意图注入 > 多轮注入 > 原始 query
4. `_create_executor()` 中拼接 system prompt 时追加意图指令

关键代码变更：

```python
async def stream_chat(self, query: str, session_id: str,
                      user_id: str = "default_user",
                      intent_result=None) -> AsyncIterator[dict]:
    """流式执行 Agent 对话。

    Args:
        intent_result: IntentResult | None，意图分类结果
    """
    memory_svc = ConversationMemoryService.get_shared()
    chat_history = memory_svc.load_context(session_id)

    # ★ 构建 agent_input（意图注入 + 多轮注入）
    agent_input = query

    if intent_result and intent_result.action_directive:
        # 意图识别成功 → 注入精确的硬约束指令
        agent_input = intent_result.action_directive + "\n\n" + query
        logger.info(
            f"【Agent】注入意图指令: intent={intent_result.intent}, "
            f"confidence={intent_result.confidence:.2f}"
        )
    elif chat_history:
        # 无意图识别结果 + 多轮对话 → 使用原有逻辑
        agent_input = (
            "[⚠️ 本轮必须调用 knowledge_search 检索知识库，"
            "禁止基于历史对话中的内容直接作答]\n\n" + query
        )

    # ... 其余逻辑不变
```

---

### 阶段 4：Agent Prompt 简化（1 个文件修改，可选优化）

#### Step 4.1: 修改 `app/config/prompts/agent.txt`

将第 2 节"意图分类"简化为"工具调用规则"，减少主 LLM 的决策负担：

```diff
- ## 2. 意图分类（最高优先级，收到消息第一步）
- 收到用户消息后，**先分类，再行动**。无法明确判定时，一律执行强制检索。
- ### 2.1 强制检索 → 调用 `knowledge_search`
- - 知识问答：私有文档相关的事实、概念、操作、业务数据、内部规范
- - 文档操作：查找、引用、总结、提取、改写已上传文档内容
- - 信息处理：对知识库内容进行对比、归纳、梳理、统计
- - 模糊意图：上下文缺失、指代不明
- - 专有名词：提及项目、工单、配置、内部流程、上传文件等
- ### 2.2 免检索直答 → 禁止调用任何工具
- - 闲聊问候（你好、谢谢、再见）
- - 能力问询（你能做什么）
- - 澄清确认（要求复述）
- - 无效输入（单字、乱码、纯符号）

+ ## 2. 工具调用规则
+ 收到 [SYSTEM OVERRIDE] 指令时，严格按照指令执行工具调用：
+ - NEED_RAG → 必须首先调用 knowledge_search
+ - NO_RAG → 跳过 knowledge_search，根据 query 直接作答或调用其他工具
+ 未收到 [SYSTEM OVERRIDE] 时，默认需要调用 knowledge_search 检索后再作答。

- 3. **每轮必须检索**：无论对话历史中已有多少相关内容，每轮对话都必须重新调用 `knowledge_search`
-    - 历史中的 AI 回答是**过去**的检索结果，知识库可能已更新，绝对不可替代本轮检索
-    - 常见错误：用户第 3 轮问"X 的优缺点"，历史中第 1 轮 AI 详细回答过 → ❌ 直接基于历史作答
-    - 正确做法：用户第 3 轮问"X 的优缺点" → ✅ 重新调用 `knowledge_search("X 优缺点")`

+ 3. **每轮必须检索**：收到 NEED_RAG 指令或未收到指令时，每轮对话都必须重新调用 `knowledge_search`。
+    历史中的 AI 回答是**过去**的检索结果，不可替代本轮检索。
```

---

## 5. 文件变更汇总

| # | 文件 | 操作 | 说明 |
|---|------|------|------|
| 1 | `app/intent/__init__.py` | **新建** | 模块导出 |
| 2 | `app/intent/intent_config.py` | **新建** | 配置常量 |
| 3 | `app/intent/intent_classifier.py` | **新建** | 核心分类器 |
| 4 | `app/config/prompts/intent_classifier.txt` | **新建** | 分类 prompt 模板 |
| 5 | `app/config/prompt.yaml` | **修改** | 注册 intent_classifier 模板 |
| 6 | `app/utils/factory.py` | **修改** | 新增 `create_intent_model()` |
| 7 | `app/router/chat_service.py` | **修改** | Agent 模式入口插入意图识别 |
| 8 | `app/agent/agent_service.py` | **修改** | 接收 intent_result，动态注入 prompt |
| 9 | `app/config/prompts/agent.txt` | **修改** | 简化意图分类章节（可选） |
| 10 | `.env` | **修改** | 新增意图识别环境变量 |

**无新增第三方依赖。** 全部复用现有 `langchain-openai`、`langchain-community`。

---

## 6. 降级策略

| 场景 | 策略 | 后果 |
|------|------|------|
| `intent_enabled=false` | 跳过意图识别，走原有流程 | 回退到现有行为 |
| 意图模型初始化失败 | 返回 NEED_RAG + 空 `action_directive` | 走原有 Agent 流程（有 recency bias 注入兜底） |
| 意图 LLM 超时 | 返回 NEED_RAG + 空 `action_directive` | 同上 |
| LLM 返回非 JSON | 返回 NEED_RAG + 空 `action_directive` | 同上 |
| 置信度低于阈值 | 降级 NEED_RAG + 空 `action_directive` | 同上 |
| 缓存命中 | 直接返回缓存结果 | 零额外延迟 |

**核心原则：宁可误判为 NEED_RAG 多调一次工具，不可误判为 NO_RAG 导致漏检。**

---

## 7. 测试用例

| # | 场景 | Query | History | 预期 intent | 预期行为 |
|---|------|-------|---------|-------------|---------|
| 1 | 首轮-文档查询 | "这份合同的有效期多久" | 无 | NEED_RAG | 调用 knowledge_search |
| 2 | 首轮-闲聊 | "你好" | 无 | NO_RAG | 直接作答，不调用工具 |
| 3 | 首轮-能力问询 | "你能做什么" | 无 | NO_RAG | 直接作答，不调用工具 |
| 4 | 多轮-指代消解 | "它的违约金呢" | 上轮提到"XX合同" | NEED_RAG | search_query="XX合同违约金" |
| 5 | 多轮-追问 | "再详细一点" | 上轮在讨论文档A | NEED_RAG | 调用 knowledge_search |
| 6 | 多轮-闲聊穿插 | "谢谢" | 上轮在讨论文档 | NO_RAG | 直接作答 |
| 7 | 模糊输入 | "嗯" | 无 | NEED_RAG | 兜底检索 |
| 8 | 意图识别关闭 | "你好" | 无 | — | 走原有 Agent 流程 |
| 9 | 意图超时降级 | "合同有效期" | 无 | NEED_RAG | 走原有 Agent 流程 |

---

## 8. 实施顺序

| 阶段 | 步骤 | 预计工时 | 依赖 |
|------|------|---------|------|
| 1 | Step 1.4 核心分类器 | 30min | 1.2, 1.3 |
| 1 | Step 1.5 factory 模型 | 10min | — |
| 1 | Step 1.1 prompt 模板 | 10min | — |
| 1 | Step 1.6 prompt.yaml | 2min | — |
| 1 | Step 1.7 .env 配置 | 2min | — |
| 2 | Step 2.1 chat_service | 15min | 阶段1 |
| 3 | Step 3.1 agent_service | 15min | 阶段1 |
| 4 | Step 4.1 agent.txt 简化 | 10min | 阶段3 |
| 5 | 端到端测试 | 20min | 全部 |

**总计：约 2 小时**

---

## 9. 与 v1 plan 的关键差异

| 维度 | v1 plan | v2 plan（本文件） |
|------|---------|-------------------|
| 分类粒度 | 6 类（rag/document/coreference/chitchat/capability/ambiguous） | 2 类（NEED_RAG/NO_RAG） |
| 识别模型 | qwen3-turbo | DeepSeek 小版本（deepseek-chat） |
| 配置位置 | chroma.yaml | .env（与项目现有配置风格一致） |
| NO_RAG 处理 | 新增 `_handle_direct_answer()` 独立方法 | 仍走 Agent 流程，通过 action_directive 约束 |
| 模块目录 | `app/utils/intent_classifier.py` | `app/intent/` 独立目录 |
| 降级 action_directive | 无 | 空字符串 → 走原有 recency bias 注入 |
| agent.txt 改动 | 简化意图分类章节 | 同等简化，但保留更多原有结构 |
