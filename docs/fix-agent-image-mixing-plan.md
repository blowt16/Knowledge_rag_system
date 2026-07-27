# Agent 模式图文混合回答修复实施方案

## 问题回顾

Agent 模式下，LLM 检索知识库后不在回答中穿插引用扫描件的图片，但日志显示图片路径已正确提取。根因是**图片 URL 与文本上下文分离**，LLM 无法有效关联。

---

## 根因与修复对照表

| # | 根因 | 严重程度 | 修复策略 |
|---|------|---------|---------|
| 1 | 图片与文本上下文分离 | 🔴 核心 | 将图片 URL 注入到每段文档文本的紧后方 |
| 2 | Attention Dilution（末尾图片被长文本淹没） | 🔴 核心 | 删除"末尾集中追加图片"逻辑，改为逐文档 inline 注入 |
| 3 | System prompt 指令矛盾（"勿集中堆放末尾" vs 工具实际集中堆放） | 🟡 加剧 | 修复后工具不再集中堆放，矛盾自动消除 |
| 4 | 每文档图片未独立标注（多图时无法对应） | 🟡 功能缺失 | 逐文档独立构建图片 markdown，每段文本只附带自己的图 |
| 5 | `_format_docs()` 完全没有图片逻辑 | 🟡 遗漏 | 补充图片注入逻辑 |

---

## 修改范围

### 文件 1：`app/agent/agent_service.py`（核心修改）

**位置**：`knowledge_search` 工具内部，第 109-128 行

**现状**：
```python
# 格式化原始文档内容
lines = []
for i, doc in enumerate(docs):
    src = doc.metadata.get("original_filename", "未知")
    page = doc.metadata.get("page", "")
    chapter = doc.metadata.get("current_chapter", "")
    header = f"[来源: {src}"
    if page:
        header += f", 第{page}页"
    if chapter:
        header += f", {chapter}"
    header += "]"
    lines.append(f"{header}\n{doc.page_content[:max_chars]}")  # ← 不检查 image_paths
answer = "\n\n".join(lines)
# 将图片 Markdown 注入工具返回结果
from app.rag.rag_service import RAGService
img_md_lines = RAGService._build_image_markdown(docs)
if img_md_lines:
    answer += "\n\n=== 附：检索结果含以下图片（只能引用这些URL，严禁修改或编造） ===\n"
    answer += "\n".join(img_md_lines)  # ← 所有图片集中末尾
return answer
```

**改为**（参考 `_generate_summary` 的做法）：
```python
# 格式化原始文档内容（图片紧跟对应文档，而非集中末尾）
from app.rag.rag_service import RAGService

lines = []
for i, doc in enumerate(docs):
    src = doc.metadata.get("original_filename", "未知")
    page = doc.metadata.get("page", "")
    chapter = doc.metadata.get("current_chapter", "")
    header = f"[来源: {src}"
    if page:
        header += f", 第{page}页"
    if chapter:
        header += f", {chapter}"
    header += "]"
    line = f"{header}\n{doc.page_content[:max_chars]}"

    # 将该文档关联的图片紧跟其后注入（根因 1/2/4 修复）
    image_paths = doc.metadata.get("image_paths", [])
    if image_paths:
        img_md = RAGService._build_image_markdown([doc])
        if img_md:
            line += "\n\n--- 附：该段资料含以下图片（只能引用这些URL，严禁修改或编造） ---\n"
            line += "\n".join(img_md)

    lines.append(line)

answer = "\n\n---\n\n".join(lines)  # 分隔符与 _generate_summary 保持一致
return answer
```

**变更要点**：
1. `from app.rag.rag_service import RAGService` 移到循环前（原来在末尾才 import）
2. 循环内逐文档检查 `image_paths`，有图片则紧跟 doc 文本注入 markdown
3. 删除原来末尾集中追加图片的代码块（第 123-128 行）
4. 分隔符从 `"\n\n"` 改为 `"\n\n---\n\n"` 与 `_generate_summary` 一致

---

### 文件 2：`app/rag/rag_service.py`（同步修复）

**位置**：`_format_docs()` 方法，第 228-244 行

**现状**：
```python
def _format_docs(self, documents: list) -> str:
    if not documents:
        return ""
    max_chars = get_config("chunk_size", 500)
    lines = []
    for i, doc in enumerate(documents):
        source = doc.metadata.get("original_filename", "未知")
        page = doc.metadata.get("page", "")
        chapter = doc.metadata.get("current_chapter", "")
        header = f"[来源: {source}"
        if page:
            header += f", 第{page}页"
        if chapter:
            header += f", {chapter}"
        header += "]"
        lines.append(f"{header}\n{doc.page_content[:max_chars]}")  # ← 无图片
    return "\n\n---\n\n".join(lines)
```

**改为**：
```python
def _format_docs(self, documents: list) -> str:
    if not documents:
        return ""
    max_chars = get_config("chunk_size", 500)
    lines = []
    for i, doc in enumerate(documents):
        source = doc.metadata.get("original_filename", "未知")
        page = doc.metadata.get("page", "")
        chapter = doc.metadata.get("current_chapter", "")
        header = f"[来源: {source}"
        if page:
            header += f", 第{page}页"
        if chapter:
            header += f", {chapter}"
        header += "]"
        line = f"{header}\n{doc.page_content[:max_chars]}"

        # 将该文档关联的图片紧跟其后注入（根因 5 修复）
        image_paths = doc.metadata.get("image_paths", [])
        if image_paths:
            img_md = RAGService._build_image_markdown([doc])
            if img_md:
                line += "\n\n--- 附：该段资料含以下图片（只能引用这些URL，严禁修改或编造） ---\n"
                line += "\n".join(img_md)

        lines.append(line)
    return "\n\n---\n\n".join(lines)
```

**变更要点**：
1. 循环内逐文档检查 `image_paths`，与 `_generate_summary` 保持一致
2. `_format_docs` 是 `@staticmethod`，调用 `RAGService._build_image_markdown` 需要使用类名（静态方法本身已经是 `@staticmethod`，可以 `self._build_image_markdown` 或 `RAGService._build_image_markdown`，但 `_format_docs` 也是 static，所以用 `RAGService._build_image_markdown` 更安全）

**注意**：`_format_docs` 是 `@staticmethod`，无法用 `self._build_image_markdown`，需要改为 `RAGService._build_image_markdown([doc])`。

---

## 全局校验（CLAUDE.md 约束）

修改完成后需执行：

```bash
# 1. 全局检索 image_paths 关键词，确认所有引用点已覆盖
grep -rn "image_paths" app/ --include="*.py"

# 2. 全局检索 _build_image_markdown 调用点
grep -rn "_build_image_markdown" app/ --include="*.py"

# 3. 全局检索 _format_docs 调用点，确认所有调用路径受益
grep -rn "_format_docs" app/ --include="*.py"

# 4. 全局检索 image_paths 出现在格式化/构建文本的上下文
grep -rn "doc\.metadata" app/ --include="*.py" | grep -v ".venv"
```

预期需要关注的调用链：
- `knowledge_search` 工具 → `RAGService.search(skip_summary=True)` → `_format_docs()` → ✅ 已修复
- `RAGService.search(skip_summary=False)` → `_generate_summary()` → ✅ 本身已正确
- `chat_service._handle_rag_stream` → `RAGService.search(skip_summary=False)` → ✅ 本身已正确

---

## 预期效果

修复后，Agent 模式下 knowledge_search 工具返回给 LLM 的内容将从：

```
[来源: 操作系统电子书.pdf, 第82页]
...文本内容...

[来源: 操作系统电子书.pdf, 第83页]
...文本内容...

[来源: 操作系统电子书.pdf, 第85页]
...文本内容...

=== 附：检索结果含以下图片 ===
![操作系统电子书.pdf](http://.../p82_i55.jpg)
```

变为：

```
[来源: 操作系统电子书.pdf, 第82页]
...文本内容...

--- 附：该段资料含以下图片（只能引用这些URL，严禁修改或编造） ---
![操作系统电子书.pdf](http://.../p82_i55.jpg)

---

[来源: 操作系统电子书.pdf, 第83页]
...文本内容...

---

[来源: 操作系统电子书.pdf, 第85页]
...文本内容...
```

第 82 页的图片紧跟在第 82 页文本之后，LLM 能够直接将图文关联起来，在回答中自然穿插引用。

---

## 风险评估

| 风险 | 概率 | 缓解措施 |
|------|------|---------|
| 工具返回内容变长，超出 LLM 上下文窗口 | 低 | 图片 markdown 本身很短（~100 字节），多个文档才可能有明显增长 |
| 系统 prompt 中图片引用相关指令需要调整 | 低 | agent.txt 第 42 行"勿集中堆放末尾"修复后自动一致，无需改动 |
| RAG 模式 regress（`_generate_summary` 不受影响） | 极低 | `_generate_summary` 图片逻辑不变，`_format_docs` 的调用者都是 Agent 路径 |

---

## 验证方法

1. 上传一个含图片的 PDF 扫描件
2. 在 Agent 模式下提问涉及图片所在页面的问题
3. 观察 LLM 回答中是否包含 `![](http://...)` 格式的图片引用
4. 检查日志中图片引用是否与文本来源对应正确
