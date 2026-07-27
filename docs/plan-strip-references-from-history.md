# Plan: AI 回答存入历史前剥离参考来源

## 问题

每轮 AI 回答末尾拼接了 `📚 参考来源：操作系统电子书.pdf (第82页)...` 后存入历史。在多轮对话中，这些标注了"来源"的答案在上下文里充当了"伪知识库"，LLM 看到历史中有详实内容便倾向于跳过 `knowledge_search` 直接作答。

## 修改范围

### 文件 1: `app/agent/agent_service.py` — `stream_chat()` finally 块

**当前** (第 312-319 行):
```python
finally:
    saved_answer = accumulated or ""
    if agent_references:
        agent_references.sort(...)
        ref_text = "\n\n---\n**📚 参考来源：**\n" + "\n".join(...)
        saved_answer += ref_text          # ← 参考来源混入持久化数据
    if memory_svc.append_messages(session_id, query, saved_answer):
```

**改为**:
```python
finally:
    saved_answer = accumulated or ""      # ← 只存纯回答，不拼接参考来源
    if memory_svc.append_messages(session_id, query, saved_answer):
```

> 参考来源仍通过 SSE `references` 事件推送给前端展示（第 268-270 行），仅改变持久化内容。

### 文件 2: `app/router/chat_service.py` — `_handle_rag_stream()` finally 块

**当前** (第 150-158 行):
```python
finally:
    saved_answer = answer or "未找到相关内容"
    ref_sources = locals().get("sources", [])
    if ref_sources:
        ref_text = "\n\n---\n**📚 参考来源：**\n" + "\n".join(...)
        saved_answer += ref_text          # ← 同上
    if self._memory.append_messages(session_id, query, saved_answer):
```

**改为**:
```python
finally:
    saved_answer = answer or "未找到相关内容"  # ← 只存纯回答
    if self._memory.append_messages(session_id, query, saved_answer):
```

## 行为变化对照

| 场景 | 改前 | 改后 |
|------|------|------|
| 前端当前页显示 | `references` SSE 事件 → 参考来源正常展示 | 不变 |
| 前端刷新/重载 | 从历史加载 → AI 回答末尾带参考来源 | 从历史加载 → AI 回答纯文本 |
| 第 N 轮 LLM 上下文 | 前 N-1 轮回答含参考来源标签 | 前 N-1 轮回答不含参考来源标签 |
| 日志 `_log_agent_refs` | 正常输出 | 不变 |

## 验证

1. 多轮对话 ≥ 3 轮，每轮检查 Agent 是否调用 `knowledge_search`
2. 查询日志确认 `tool_start: knowledge_search` 事件每轮均触发
3. 前端确认参考来源仍在 SSE 流中正常展示

## 风险

| 风险 | 评估 |
|------|------|
| 前端刷新后看不到参考来源 | 低 — 参考来源是会话内实时信息，刷新后用户通常不再关注历史记录的来源标注 |
| 日志引用信息丢失 | 无 — `_log_agent_refs` 不受影响 |
