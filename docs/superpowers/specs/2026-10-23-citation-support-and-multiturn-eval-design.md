# 引用支持度校验与多轮评测方案

- 日期：2026-10-23
- 状态：设计稿（待评审）
- 范围：问答引用校验、Prompt 约束、批量评测单/多轮模式、多轮用例生成与手工录入
- 相关分支：`refactor/campus-rag`

---

## 1. 背景与问题

### 1.1 事故现场

会话 `5fe87fe898e544bb8e1e568f80a2e503`，2026-10-19 13:13:53（UTC）：

- 用户问：**“我是艺术类专业的学生需要达到什么条件”**
- 模型答（节选）：

  > 专科起点本科的毕（结）业证书中注明“专科起点本科学习”**[1][2]**。

- `[1]`、`[2]` 实际指向《02_学位授予文件》第 2、3 页。

核对结果：

| 核对项 | 结论 |
|---|---|
| 这句话在不在知识库 | **在**。《04 普通高等教育本科学生学籍管理规定（2025修订）》第四十五条、《06 本科生管理规定（桂电学2019-24号）》第三十五条 |
| 为什么直接搜不到 | PDF 原文在“专”“科”之间换行：`……注明“专`↵`科起点本科学习”。整句复制搜索会失败 |
| 被引用的《02》里有没有 | **没有**，全文 0 处“专科起点” |
| 本轮检索证据有没有 | **没有**。13:13 这轮 rerank top-5 是《02》三个片段 + 《12》一个片段 + 《02》一个片段；《06》片段排第 6，被挤出 |
| 上一轮说过吗 | **说过**。12:59 的回答末尾就是同一句，当时 `[5]` 正确指向《06》p12 |

### 1.2 根因链

1. 12:57 / 12:59 两轮问“毕业/学位条件”，《06》的“学业证书管理”片段进了证据，模型答出这句话，引用正确；
2. 13:13 追问“艺术类专业”，rerank 只保留《02》和《12》，《06》片段掉出证据；
3. 模型把上一轮答案的结尾整段沿用，但拼历史时 `[n]` 被系统剥掉（`backend/app/services/context_service.py:299`）；
4. 模型重新编号，把这句话挂到本轮看起来最相关的 `[1][2]`；
5. `cite.py` 只按编号位置贴证据，不判断“这段文字是否支持这句话”；
6. `verify_report` 只检查编号是否越界，`cited_claims=5/5` 全部通过，没有报警。

### 1.3 定性

- **不是内容幻觉**：句子在语料里真实存在；
- **是引用幻觉 / 归因错误**：把一句话归到了不包含它的文件；
- **同时是上下文残留**：这句话与本次问题无关，是上一轮“专升本”答案的尾巴；
- **校验盲区**：引用支持关系从未被验证。

---

## 2. 目标与非目标

### 2.1 目标

1. 在问答链路里识别“引用不支持”的结论句，前端置灰/提示，并落库，刷新历史仍可见；
2. 用 Prompt 明确限制“历史事实不得挂本轮编号”，减少上下文残留；
3. 批量评测支持**单轮 / 多轮执行模式**，多轮模式逐轮回放并计算逐轮结构指标与 RAGAS；
4. 多轮用例支持**手工录入**和**LLM 基于相邻 2~3 个 chunk 生成**，每轮都带标准答案；
5. 让现有 15 条多轮题可以被编辑补答案，而不是只能靠 fixture 导入。

### 2.2 非目标

- 不在线上热路径引入 LLM/NLI 的语义蕴含判定（第一版只做字面规则）；
- 不自动删除或改写“引用不支持”的句子（流式输出无法撤回，只标注）；
- 不自动给现有 15 条多轮题补标准答案（提供编辑能力，人工补）；
- 不改变 RAGAS“整批跑完后一次性计算”的机制；
- 不改变单轮事实题的出题方式；单轮与多轮的标准答案校验统一为“逐句原文拼接”。

---

## 3. 关键决策

| 决策点 | 选择 |
|---|---|
| 批量评测下拉语义 | **执行模式**：单轮只跑 `question`；多轮对带 `turns` 的用例逐轮回放 |
| 引用灰标持久化 | **落库**：`messages.verify_report`，刷新历史仍显示 |
| 支持度判定 | **字面规则**：中文二元组 + 数字/专名匹配，不调模型 |
| 多轮 RAGAS 口径 | **所有进入检索分支且未拒答的轮次都送**；每轮一条样本；整批跑完后一次性计算 |
| 多轮生成材料 | **相邻 2~3 个 chunk 窗口** |
| 生成提示词 | **可选附加约束**：只影响选题与问法，不覆盖“逐句原文拼接”等硬校验；单轮/多轮都支持 |
| 生成的标准答案 | **逐句原文拼接（方案 A）**：答案可由 1~3 个原文分句组成；每个实质分句必须逐字命中窗口，整段不要求连续 |
| 手工录入的标准答案 | **只提示，不硬拦** |
| 多轮四张 RAGAS 卡 | 按**轮次**平均，标注“N 轮有分”；各指标分母可不同 |

> **已确认**：采用严格逐句拼接（方案 A）。单轮/多轮共用；不引入语义相似或改写判定。

---

## 4. 设计一：引用支持度校验

### 4.1 数据结构

新增 `UnsupportedClaim`（`backend/app/schemas/chat.py`）：

```python
class UnsupportedMarker(BaseModel):
    marker: int
    chunk_id: str
    reason: Literal["no_overlap", "number_mismatch", "weak_overlap"]
    score: float             # 该编号对应片段的 unit_recall

class UnsupportedClaim(BaseModel):
    sentence: str
    char_start: int          # 相对渲染前原始答案，Unicode 码点
    char_end: int
    markers: list[int]       # 该句引用的全部合法编号
    unsupported_markers: list[int]  # 其中不支持的编号
    reason: Literal["no_overlap", "number_mismatch", "weak_overlap"]
    score: float             # 最好的 unit_recall，便于调阈值
    cited_chunk_ids: list[str]
```

扩展 `VerifyReport`：

```python
class VerifyReport(BaseModel):
    total_claims: int = 0
    cited_claims: int = 0                 # 有合法 [n] 的结论句（语义不变）
    supported_claims: int = 0             # 有合法 [n] 且至少一条被引片段支撑
    citation_support_rate: float | None = None
    invalid_markers: list[InvalidMarker] = []
    uncited_claims: list[UncitedClaim] = []
    # 句子级：只有所有被引片段都不支持时才进来 → 前端置灰
    unsupported_citations: list[UnsupportedClaim] = []
    # 编号级：部分支持时，失败的那个编号也在这里 → 前端角标提示
    unsupported_markers: list[UnsupportedMarker] = []
```

向后兼容：新字段都有默认值；老消息没有 `verify_report` 时前端不标注。

### 4.2 判定算法

输入：原始答案、`evidence`（顺序即 `[n]` 的编号顺序）。

1. 按现有 `_SENTENCE_RE` 切句，复用 `is_conclusion_sentence()` 只取结论句；
2. 对每个带合法 `[n]` 的结论句：
   - 取该句引用的所有 `evidence[n-1].text`；
   - 归一化：`NFKC` 后去掉空白、标点、符号，保留中文/字母/数字；
   - 提取单元：
     - 中文连续段的**二元组**（`"转专业"` → `转专`、`专业`）；
     - 字母/数字 token（长度 ≥2；数字无论长短都保留）；
   - 对每条被引片段计算：
     - `unit_recall = |句子单元 ∩ 片段单元| / |句子单元|`；
     - `number_ok`：句子里所有数字都必须出现在片段里；
   - 判定为“该片段支持”：
     - `number_ok == True` 且
     - `unit_recall >= 0.4`，或句子与片段存在 ≥4 字的中文公共片段；
   - 如果句子内容单元少于 3 个，**不判**（保守，避免误标）；
   - 每条被引片段**单独判定**：
     - 失败的编号写入 `unsupported_markers`（即使句子整体 supported，前端也要能在角标上提示）；
     - 句子级 `unsupported_citations` **只在所有被引片段都不支持时**生成；
     - 任意一条支持 → 该句算 supported。
   - 公共片段用 **4-gram 集合**判断，不要用 LCS（避免 O(n²)）。
3. `reason` 取值：
   - 数字对不上 → `number_mismatch`；
   - 有数字匹配但 `unit_recall < 0.4` → `weak_overlap`；
   - 其余 → `no_overlap`。
4. `citation_support_rate = supported_claims / (supported_claims + len(unsupported_citations))`；分母为 0 时 `None`。
   这是**句子级**比率；编号级的错误单独看 `unsupported_markers`，不要混进这个分母。

阈值初版写成常量（`SUPPORT_UNIT_RECALL = 0.4`、`SUPPORT_MIN_UNITS = 3`、`SUPPORT_LONG_UNIT = 4`），后续用标注样本校准；校准前只作为提示，不阻断回答。

### 4.3 接入点

- 新模块 `backend/app/graph/nodes/citation_support.py`：纯函数，便于单测；
- `backend/app/graph/nodes/cite.py`：
  - `build_verify_report()` 调用支持度检查；
  - `build_citations()` 不变；
  - `cite_node()` 返回的 `verify_report` 带新字段。
- SSE：`chat_service` 的 `verify` 事件自动带上新字段，无需新增事件。

### 4.4 持久化

新增迁移 `backend/migrations/005_citation_and_dialogue_mode.sql`：

```sql
ALTER TABLE messages ADD COLUMN IF NOT EXISTS verify_report JSONB;

ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS dialogue_mode TEXT NOT NULL DEFAULT 'single';

DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'eval_runs_dialogue_mode_check'
                      AND conrelid = 'eval_runs'::regclass) THEN
        ALTER TABLE eval_runs
            ADD CONSTRAINT eval_runs_dialogue_mode_check
            CHECK (dialogue_mode IN ('single', 'multi'));
    END IF;
END $mig$;
```

> 两个变更放同一个迁移，避免为了一个小列再走一次迁移链。
> ⚠️ **CHECK 不能内联在 `ADD COLUMN IF NOT EXISTS` 里**：列已存在时整条语句跳过，
> 约束就永远补不上。与 `004` 同一写法：先加列，再用 `DO` 块按约束名补 `CHECK`。

写入：

- `conversation_service.append_messages()` 增加 `verify_report` 参数，写入助手消息；
- `chat_service._persist()` 传入 `final["verify_report"].model_dump()`（没有则 `None`）；
- 落库失败仍只记日志，不影响回答（保持现有容错）。

读取：

- `conversation_service.get_messages()` 查询 `verify_report`；
- `MessageItem` 增加 `verify_report: dict | None`；
- 前端 `toTurns()` 把 `answer?.verify_report` 放进 `draft.verify`；
- `ChatPage` 中“历史会话灰标不再显示”的注释删除，改为“落库后历史也显示”。

### 4.5 前端

- `frontend/web/src/api/types.ts`：`VerifyReport` 增加 `unsupported_citations`、`unsupported_markers`、`supported_claims`、`citation_support_rate`；
- `frontend/web/src/markdown/uncitedAnnotation.ts`：
  - 保持 `annotateTree` / `remarkUncited` 现有签名不变；
  - 增加可选的 `className` / `title` 参数（默认仍是 `uncited` / 现有文案）；
  - 新增 `remarkUnsupported`，使用 `unsupported` class 与文案
    “所引资料未包含该结论的关键内容，建议核对”。
- `MarkdownAnswer.tsx`：
  - 接收 `verify.unsupported_citations`（句子级）与 `verify.unsupported_markers`（编号级）；
  - 插件顺序：`remarkUncited` → `remarkUnsupported` → `remarkCitations`
    （角标插件会拆分文本节点、丢掉 position，必须最后跑）；
  - 句子级：只有 `unsupported_citations` 进入 `remarkUnsupported`，整句琥珀色下划线；
  - 编号级：角标组件读 `unsupported_markers` 的 marker 集合，给对应角标加 `unsupported` class，悬停提示“编号 [n] 引用的资料未包含该句的关键内容”；
  - 汇总条两行：
    - “本回答含 N 处引用与原文不符”（编号级数量）；
    - “其中 M 句的所有引用均不支持”（句子级数量）；
  - 偏移校验失败时与现有 uncited 一样降级为消息级提示，不硬标。
- `index.css`：
  - `.unsupported { text-decoration: underline wavy #b45309; }`
  - `.cite-badge.unsupported { box-shadow: 0 0 0 1px #b45309; }`
  - `.unsupported-summary` 与 `.uncited-summary` 同布局，琥珀色。

### 4.6 测试

新增 `backend/tests/unit/test_citation_support.py`：

- 事故复现：句子来自《06》，证据是《02》→ `unsupported_citations` 命中；
- 同一句引用《06》→ 支持；
- 数字不匹配 → `number_mismatch`；
- 短句（<3 个内容单元）→ 不标记；
- **部分支持**：句子引用 `[1][2]`，`[1]` 支持、`[2]` 不支持 → `unsupported_markers` 含 2，`unsupported_citations` 为空（不置灰）；
- **全部不支持**：两个编号都不支持 → `unsupported_citations` 含该句（置灰），`unsupported_markers` 含 1、2；
- 偏移可切片且逐字等于 `sentence`。

扩展 `backend/tests/unit/test_cite.py`：

- `cite_node` 同时产出 `citations`、`unsupported_citations` 与 `unsupported_markers`；
- 拒答路径仍不产出任何引用/校验。

扩展 `backend/tests/integration/test_conversations_api.py`：

- `messages` 返回助手消息的 `verify_report`（含 `unsupported_citations` / `unsupported_markers`）；
- 老消息没有该列时返回 `None`，前端不标注。

---

## 5. 设计二：Prompt 硬约束

### 5.1 改动

在 `backend/app/config/prompts/generate.txt` 的“硬约束”中新增第 6 条：

> **历史中的事实只能用于理解指代与省略。** 某个事实如果本轮证据不支持，
> 不得写入答案；确需提及时必须注明「上一轮回答中的内容」，且不得挂本轮 `[n]` 编号。
> 每个结论句的事实必须能在本轮证据中找到依据。

### 5.2 测试

更新 `backend/tests/unit/test_prompts.py` 的 `GENERATE_HARD_RULES`：

```python
"历史事实不得冒用本轮编号": ["历史中的事实", "不得挂本轮"],
"结论句必须有本轮依据": ["每个结论句的事实", "本轮证据"],
```

Prompt 只是提醒；真正的兜底是第 4 节的支持度校验。

---

## 6. 设计三：批量评测单轮 / 多轮

### 6.1 UI

`frontend/web/src/views/admin/eval/EvalRunsPage.tsx`：

- 在“角色”旁新增：

  ```text
  对话模式  [单轮 ▾]
            单轮
            多轮
  ```

- 默认“单轮”；
- 提交时带 `dialogue_mode`；
- 提示文案：

  > 单轮：每道题只跑第一问。
  > 多轮：带 `turns` 的多轮题逐轮回放；普通单轮题不受影响。

- 任务表增加“模式”列或在评测集名称旁显示“单轮/多轮”徽标；
- 报告弹窗标题行显示“对话模式：多轮”。

### 6.2 接口与存储

- `EvalRunRequest` 增加：

  ```python
  dialogue_mode: Literal["single", "multi"] = "single"
  ```

- `eval_runs.dialogue_mode` 列（见 4.4 迁移）；
- `start_run()` 写入该列；**不放进 `config`**（非空 `config` 会被判成消融运行）；
- `EvalRunSummary` / `EvalRunDetail` 增加 `dialogue_mode`；
- `list_runs()` / `get_run()` 的 SELECT 与响应都带 `dialogue_mode`；老 run 走默认值；
- 老 run 默认 `single`；对比页会显示该模式（见 6.5）。

### 6.3 Runner 执行

`backend/app/eval/runner.py`：

**单轮模式**

- 普通题：保持现有逻辑（`query=case.question`、`history=[]`）；
- 多轮题：取 `turns[0]` 作为单轮题：
  - `query = turns[0].question`（通常等于 `case.question`）；
  - `ground_truth = turns[0].ground_truth or case.ground_truth`；
  - **期望文档**取 `turns[0].expected_document`（标题）；多轮题的 case 级 `expected_doc_ids` 通常是空，不能再拿它算 Recall/MRR；
  - 这样同一套题可以公平对比单轮/多轮。

**多轮模式**

- 带 `turns` 的用例：
  - 累加 `history: list[Message]`（用户问题 + 助手答案）；
  - 每轮传 `last_route`（上一轮路由），与 `cmd_eval_multiturn` 同源；
  - **同时传 `clarify_chain` / `clarify_total`**：生产环境是从 `messages.route` 数出来的（`chat_service._clarify_counts`，`route.py:160` 会读）；评测器没有 DB 历史，必须从已回放的轮次里现算，否则澄清行为与线上不一致；
  - `session_id = f"eval-{run_id[:8]}-{case_id}"`，按用例隔离；
  - **评测不跑滚动压缩**：2~4 轮历史可控；这是与线上的已知差异，报告/文档要写明；
  - 逐轮记录：
    - `question`、`resolved_query`、`route`、`route_ok`；
    - `clarify_ok`（如果该轮有 `should_clarify`）；
    - `resolved_anchor_hit`（如果该轮有 `resolved_must_contain_any`）；
    - `retrieval_hit`、`top1_doc`、`evidence_ids`（如果该轮有 `expected_document`；比较 `document_name` 与标题，只有 `expected_doc_ids` 时回退到 doc_id 比较）；
    - `citation_support_rate`、`unsupported_citations`（句数）、`unsupported_markers`（编号数）；
    - `answer`（为控制体积，建议截断到 2000 字；报告下钻用）。
- 不带 `turns` 的用例：按单轮跑，计 1 轮。
- 现有 `backend/app/cli.py` 的 `eval-multiturn` **保留**：它只跑图前半段（路由/澄清/消解/检索），用于专项；新批量多轮模式跑完整回答 + RAGAS，不替代它。
- 逐轮结果写入 `eval_case_results.metrics["turns"]`，形如：

  ```json
  {
    "turns": [
      {
        "turn": 1,
        "question": "...",
        "resolved_query": "...",
        "route": "knowledge",
        "route_ok": true,
        "clarify_ok": true,
        "resolved_anchor_hit": null,
        "retrieval_hit": true,
        "top1_doc": "01_...",
        "citation_support_rate": 1.0,
        "unsupported_citations": 0,
        "unsupported_markers": 0,
        "ragas_faithfulness": 0.91,
        "ragas_answer_relevancy": 0.88,
        "ragas_context_precision": 0.8,
        "ragas_context_recall": 0.75
      }
    ]
  }
  ```

### 6.4 RAGAS 送样

对每一轮（单轮题就是它唯一一轮）满足以下条件就送：

1. `route == "knowledge"`；
2. `not refused`；
3. 有答案、有检索上下文。

样本：

```python
{
  "user_input": resolved_query or question,
  "response": answer,
  "retrieved_contexts": [render_chunk_block(c, i) for i, c in enumerate(evidence, 1)],
  "reference": turn.ground_truth,   # 没有就 None
}
```

- `user_input` 必须用消解后的 `resolved_query`：追问原句（“那需要什么条件？”）单独看没有主题，会误伤答案相关性；
- 所有样本在全部用例跑完后**一次性** `score_samples()`，机制不变；
- **样本映射**：用 `sample_map: list[tuple[case_index, turn_index]]` 记录每条样本属于哪条用例、哪一轮；`score_samples()` 的 `rows` 与样本同序，按 `sample_map` 回填到 `metrics.turns[i].ragas_*`。当前只有案例级的 `ragas_index`，必须替换，否则一条多轮用例的多条样本会覆盖同一行；
- **成本**：样本数 = 所有 knowledge 且未拒答的轮次数；多轮题越多、轮数越多，判官成本线性增长。UI 显示预计样本数，不做硬上限，沿用现有单批 600s / 整轮 3600s 超时。

**实测结论（2026-10-23）**：`reference=None` 时，忠实度与答案相关性仍可计算；上下文精度/召回返回 `null`（内部 `KeyError('reference')` 被 `raise_exceptions=False` 吞掉）。因此：

- 有逐轮标准答案 → 四项齐全；
- 没有 → 只有两项生成侧指标，检索侧指标不计入。

### 6.5 聚合与报告

`backend/app/eval/report.py`：

- 单轮模式：保持现有 case 级聚合；
- 多轮模式：把每条用例的 `metrics.turns[*].ragas_*` **摊平**，四张卡按轮次求平均；
- 每张卡新增：
  - `scored_samples`（该指标有分的轮次数）；
  - `sample_unit`（`"turn"` 或 `"case"`）。
  保留 `scored_cases` 以兼容老轮次；前端优先读 `scored_samples`。
- 整轮新增：

  ```json
  "multi_turn": {
    "turns_total": 42,
    "turns_completed": 42,
    "route_accuracy": 0.95,
    "clarify_accuracy": 0.90,
    "retrieval_hit_rate": 0.80,
    "resolve_anchor_hit_rate": 0.85,
    "citation_support_rate": 0.92,
    "unsupported_citations": 3,
    "unsupported_markers": 5
  }
  ```

- `ragas_scored_cases` 保留（至少有一条轮次有分的用例数）；
- 新增 `ragas_scored_turns`（有分的轮次数）。

`backend/app/schemas/eval.py`：

- `EvalReportMetric` 增加 `scored_samples`、`sample_unit`（取值 `case` / `turn`）；
  `scored_samples` 是该单位下的有分数量，`scored_cases` 保留以兼容老轮次；
- `EvalReport` 增加 `dialogue_mode`、`multi_turn`；
- `EvalReportCase` 增加 `turns: list[dict] = []`（下钻用，含每轮 `ragas_*`、`citation_support_rate`、`unsupported_markers`）；
- `EvalCompareRow` 增加 `dialogue_mode`（批量评测 run 进对比表时显示）。

`ReportDialog.tsx`：

- 标题行显示“对话模式”；
- 多轮模式在四张卡上方增加一行结构指标：
  路由准确率 / 澄清准确率 / 检索命中率 / 消解锚点命中率 / 引用支持率 / 不支持引用数；
- 四张卡的“N 题有分”在多轮模式改成“N 轮有分”；
- 上下文两项为空时，提示“这两项需要有逐轮标准答案；当前评测集只有 M 轮有答案”，不要笼统说“没有可评分样本”；
- 逐题表格增加“轮数”列；多轮模式下，逐题行的四项分数取该用例各轮的非空均值（某指标全空则为“—”），逐轮明细放 `cases[].turns`，第一版不强制展开；
- `report.build()` 在 multi 模式下：先按轮摊平算四张卡；再按用例把各轮 `ragas_*` 求非空均值，填进逐题行，避免逐题表只有空值；
- `report._verdict()` 的分母文案按 `sample_unit` 输出：`case` 用 `ragas_scored_cases`，`turn` 用 `ragas_scored_turns`；上下文两项的空缺原因也按模式写。

**对比页（`EvalAblationPage.tsx` / `GET /compare`）**

- `eval_compare()` 的 SELECT 增加 `dialogue_mode`，`EvalCompareRow.dialogue_mode` 一并返回；
- 对比表增加“模式”列/徽标；
- 指标全集加入 `citation_support_rate`、`unsupported_markers`、`unsupported_citations`、`ragas_scored_turns`、`turns_total`；
- 单轮与多轮混在一张表时，表下提示“单轮按题、多轮按轮，分母不同，四项分数不要直接横比”；
- 老 run 没有该列 → 默认 `single`。

### 6.6 测试

扩展 `backend/tests/integration/test_eval_runner.py`：

- 单轮模式：多轮题只跑 `turns[0]`，`ground_truth` 用第一轮答案；检索命中用 `turns[0].expected_document`；
- 多轮模式：按 `turns` 顺序调用假图，历史逐轮累加，`last_route` 正确传递；
- `clarify_chain` / `clarify_total` 从已回放轮次现算并传入 state；
- 送样条件：chat/clarify 轮不送，拒答轮不送，knowledge 且未拒答的轮都送；
- `sample_map` 把每条 RAGAS 分数回填到正确的 `(case, turn)`；
- 单题失败仍留下原因。

扩展 `backend/tests/integration/test_eval_api.py`：

- `dialogue_mode` 缺省为 `single`；
- `multi` 正确落库并出现在详情；
- 非法值 422。

扩展 `backend/tests/integration/test_eval_compare.py`：

- 对比行带 `dialogue_mode`；
- 老 run 缺列时按 `single` 显示。

报告测试：

- 多轮模式四张卡按轮次平均；
- 有/无 `reference` 时各指标分母正确；
- 单轮模式结果与改造前一致；
- `_verdict()` 在 `sample_unit=turn` 时念“轮”，在 `case` 时念“题”。

---

## 7. 设计四：多轮用例生成与手工录入

### 7.1 数据形状

`eval_cases.turns`（JSONB，已有列）每项：

```json
{
  "question": "那需要提交哪些材料？",
  "ground_truth": "……",
  "expected_route": "knowledge",
  "should_clarify": 0,
  "expected_document": "01_本科生专业分流及转专业管理办法_桂电教2021-6号",
  "resolved_must_contain_any": ["转专业"],
  "note": "省略：原句无主题词"
}
```

用例级约定：

- `case_type = "multi_turn"`；
- `question = turns[0].question`（列表展示、单轮模式都用它）；
- `ground_truth = NULL`（每轮答案只放 `turns`）；
- 生成题 `expected_doc_ids = [document_id]`；
- 至少 2 轮。

### 7.2 手工录入

**接口**

- `EvalTurn` 模型（`schemas/eval.py`）：

  ```python
  class EvalTurn(BaseModel):
      question: str = Field(min_length=1)
      ground_truth: str | None = None
      expected_route: Literal["chat", "clarify", "knowledge"] | None = None
      should_clarify: Literal[0, 1] | None = None
      expected_document: str | None = None
      resolved_must_contain_any: list[str] | None = None
      note: str | None = None
  ```

- `EvalCaseCreate` 增加 `case_type`、`turns`；**`question` 在多轮时改为可选**：`case_type='multi_turn'` 时由 `turns[0].question` 派生（`model_validator`），不要让前端填两份；
- `EvalCasePatch` 增加 `turns`（`case_type` 不允许改）；多轮题**不允许单独改 `question`**（400，提示改 `turns`），改 `turns` 时服务端同步 `question = turns[0].question`；`turns=null` 或少于 2 轮直接拒绝；
- `EvalCaseItem` 增加 `turn_count: int = 0`（由 `turns` 长度算）；
- 新增 `GET /cases/{id}` 返回完整用例（含 `turns`），编辑弹窗用；列表不返回整段 `turns`，避免带宽膨胀；
- `list_cases()` 的搜索条件从 `question ILIKE` 扩为 `question ILIKE OR turns::text ILIKE`，否则后续轮的问题搜不到（表小，文本搜索可接受；量大再加 GIN）；
- `EvalCaseSource` 增加 `turns: list[EvalTurn]` 与 `turn_highlights: list[list[int] | None]`；`sets.case_source()` 对每轮 `ground_truth` 调 `locate_highlight(snippet, answer)`。多轮题用例级 `ground_truth` 为 NULL，不能再只高亮那一个答案。

**校验**

- `multi_turn`：`turns` 至少 2 轮；每轮 `question` 非空；`question` 由服务端同步为第一轮问题（请求里带了也忽略）；
- 每轮 `ground_truth` 允许为空。**提示由前端本地产生**（提交前检查：“第 N 轮没有标准答案，该轮上下文指标不会计入”），后端只做结构校验，不因为某轮缺答案而拒绝；
- `expected_route` 只能是 `chat/clarify/knowledge`，`should_clarify` 只能是 0/1（Pydantic `Literal`）；
- `factual`：`turns` 必须为空/`null`；多轮题请求里即使带了用例级 `ground_truth` 也忽略；
- 手工答案**不硬校验原文**：不做“不在原文里”的拦截，只在能定位源文档时由界面提示。

**存储**

- `sets.create_case()` 增加 `case_type` / `turns` 参数；
- `sets.update_case()` 的 `EDITABLE_CASE_FIELDS` 增加 `turns`；
- 写入时 `turns` 直接传 Python list（连接已注册 JSONB 编解码器，不要再 `json.dumps`）；
- 多轮题 `ground_truth` 写 `NULL`。

**UI**

`frontend/web/src/views/admin/eval/EvalSetsPage.tsx`：

- “手工录入”弹窗顶部加“单轮 / 多轮”切换；
- 多轮模式显示轮次表：
  - 每行：问题、标准答案；可增删；
  - 至少 2 行，未填答案的行标黄提示；
- 列表“问题”列：多轮题显示第一轮问题 + “多轮 N 轮”徽标；
- “标准答案”列：多轮题显示“逐轮答案（N 轮）”，不展示第一轮答案以免误解；
- 编辑多轮题：打开时 `GET /cases/{id}` 拉取 `turns`；
- “核对原文”弹窗：多轮题展示源片段（窗口文本）与各轮答案；逐轮高亮来自 `turn_highlights`。窗口可能跨页，PDF 框选只按 `source_page`（首块页码）定位，跨页部分以文本高亮为准。

### 7.3 LLM 生成多轮用例

**请求**

`EvalGenerateRequest` 增加：

```python
mode: Literal["single", "multi"] = "single"
turns: int = Field(default=3, ge=2, le=4)     # 目标轮数，不是保证
window: int = Field(default=3, ge=2, le=3)    # 窗口 chunk 数
instruction: str | None = Field(default=None, max_length=500)
```

单轮模式的请求/出题方式不变；标准答案校验改为与多轮共用的“逐句原文拼接”（见下文）。
`instruction` 对单轮、多轮都生效。
`turns` 是**目标不是保证**：模型返回 2~4 轮都接受；少于 2 轮重试/跳过。
`EvalGenerateCaseOut` 增加 `turn_count`，响应里 `cases` 每条都要带。

**生成提示词（单轮/多轮共用）**

- 界面上是一个可选文本框，不是用来替代硬规则的；
- 只影响“出什么题、怎么问、偏向哪个角度”，例如：
  - 单轮：“只出条件/时限类事实题，问法贴近学生口语，不要照抄原文句式。”
  - 多轮：“围绕转专业的申请条件与材料，生成连续追问，第二轮必须用省略。”
- 拼接方式：**单轮**非空时替换默认的 `FACTUAL_INSTRUCTION`（它本来就只描述选题与问法）；**多轮**作为额外段落附加在多轮 Prompt 中；两种情况下硬规则都不变；
- **不能覆盖**：输出 JSON 格式、每个实质分句必须逐字来自材料（逐句拼接）、单轮题不许带代词、多轮至少 2 轮；
- 生成器只产出 **knowledge 可答题**；闲聊/澄清负例仍需手工录入，不能靠这段提示词生成；
- 条数、轮数、窗口大小由界面字段控制，提示词不负责这些；
- 空字符串按 `None` 处理；超过 500 字返回 422（`Field(max_length=500)`）；
- 基础 Prompt 中把它放在“管理员额外要求”段落，并写明：
  > 额外要求只影响选题与问法；如果与下面的硬性要求冲突，一律以硬性要求为准。
- 该提示词最终会写进生成用例的 `note`（截断到 200 字，前缀“生成要求：”），方便事后核对为什么出了这道题；
- 接口仅管理员可用；即使提示词要求编造材料里没有的答案，逐句校验也会拒绝，不会入库。

**窗口构建**

1. 从向量库取该文档 chunk（`vector.get_chunks`），过滤过短片段；
2. 按 `chunk_index` 排序；
3. 用 `window` 个相邻 chunk 组成候选窗口；窗口数不足 `count` 时**允许重叠窗口**（均匀取起点），不减少生成条数；
4. 窗口文本优先按 chunk 的 `char_start/char_end` 从规范化正文合并成连续文本，避免 chunk 重叠导致重复；
5. 只有元数据缺失时才退化为拼接 + 最长前后缀去重。

**已核实**：`vector.get_chunks()` 返回的 metadata 含 `chunk_index`、`char_start`、`char_end`
（实测 `4523...:0 = 0~170`、`:1 = 170~668`，首尾相接），所以窗口合并可直接用偏移，
不必依赖拼接去重。

**Prompt（一次调用生成整段对话）**

输入：窗口文本、文档标题、目标轮数。

要求：

1. 输出 JSON：`{"turns": [{"question": "...", "ground_truth": "...", ...}]}`；
2. 第一轮自足，**不许出现代词**；
3. 后续轮必须使用代词或省略（那 / 它 / 这个 / 该 / 其 / 多久……），这是多轮题的意义；
4. 每轮 `ground_truth` 由 **1~3 个原文分句**组成；需要多句时，直接按原文用 `；` 或 `。` 拼接，不要改写、不要概括、不要添加原文没有的连接词；每个实质分句都必须是窗口材料里的逐字原文；
5. 各轮答案不能重复，至少覆盖 2 个不同事实；
6. 不许跨出窗口材料，不许用常识补全；
7. 轮数 2~4，默认 3；
8. 管理员额外要求（若提供）只作为选题/问法建议；与以上要求冲突时，以以上要求为准。

附一个 2 轮 few-shot 示例（与单轮生成同思路：一个例子比十条规则有效）。

**标准答案校验（单轮/多轮共用）**

目标：允许复杂答案由多个原文分句组成，但每个实质分句必须逐字来自材料；不允许改写、概括、编造数字。

1. **切分**：按 `。；！？\n` 切分句，**不按逗号切**（避免“可以申请”这类过短泛化片段）。去掉空段。
2. **归一化**：复用 `squeeze()`（去空白、`｜`、`：`；保留中文/字母/数字/逗号等）。
3. **分句分类**：
   - **实质分句**：归一化后长度 ≥8，或含数字，或含长度 ≥2 的英文 token；
   - **忽略分句**：其余（如“此外”），视为连接词，不校验、不计入覆盖率。
4. **通过条件（全部满足）**：
   - 至少 1 个实质分句；
   - 实质分句数 **≤3**（超过 3 句重试）；
   - 每个实质分句都满足 `squeeze(分句) in squeeze(窗口文本)`；
   - 答案里的所有数字（`\d+(?:\.\d+)?`）都出现在窗口文本里；
   - 覆盖率 `实质分句总字数 / 答案总字数 >= 0.8`；
   - 允许分句来自窗口不同位置，**不要求连续**；默认不检查顺序。
5. **明确不做**：语义相似、同义改写、逻辑组合校验；这些由 Prompt 与人工抽查兜底。

判定示例（窗口为退役复学材料）：

| 答案 | 结果 | 原因 |
|---|---|---|
| 单句原文 | 通过 | 1 个实质分句命中 |
| 三句原文拼接 | 通过 | 3 个实质分句全部命中 |
| 改写概括 | 拒绝 | 分句不是原文 |
| 一句原文 + 一句改写 | 拒绝 | 所有实质分句必须命中 |
| 带“此外，”的原文 | 拒绝（严格版） | 多了原文没有的连接词 |
| 只有“可以申请” | 拒绝 | 没有实质分句 |
| 数字改成 90 分 | 拒绝 | 数字不在原文 |
| 两个真但组合可疑 | 通过 | **规则管不了逻辑组合** |

单轮生成同样使用这套校验，只是把 `窗口文本` 换成 `chunk`；单轮 Prompt 的“答案必须是一段连续原文”同步改成“1~3 个原文分句拼接”。

**多轮结构校验（不通过就重试，最多 3 次）**

- 轮数 2~4；
- 每轮问题非空、无重复；
- 第一轮不含代词（复用 `generate.has_pronoun`）；
- 每轮 `ground_truth` 按上面的“标准答案校验”判定；
- 各轮答案去重（按 `squeeze` 后比较）。

“后续轮必须用代词/省略”**只写进 Prompt，不做硬拒绝**：像“办理流程要多久？”这种合法省略不含代词，硬拦会误伤。这条质量由多轮评测的 `resolve` / 路由指标暴露。

**入库**

- `case_type='multi_turn'`；
- `turns` 写入 JSONB；
- `question = turns[0].question`；
- `ground_truth = NULL`；
- `expected_doc_ids = [document_id]`；
- `expected_route='knowledge'`、`should_clarify=0` 由服务端补齐（模型没给也补）；
- `expected_document` 填源文档标题；
- `source='generated'`；
- `source_document_id`、`source_chunk_id`（窗口首块）、`source_page`、`source_snippet`（窗口文本）；
- `note` 写 `生成要求：<instruction>`（截断到 200 字；没有自定义提示词则为 `NULL`）。

单轮生成的入库也照此处理：`note` 记录自定义提示词，便于追溯。

**超时与并发**

- 单轮：保持 `TOTAL_TIMEOUT=120s`；
- 多轮：建议 `MULTI_TIMEOUT=180s`，并发 3，单次 `max_tokens=2000`；
- 超时返回已生成部分，`reason` 说明剩余可再点一次；
- 前端提示多轮生成更慢。

**UI**

“从文档自动生成”弹窗：

- 增加“单轮 / 多轮”切换；
- **两种模式都显示“生成提示词（可选）”文本框**：
  - 占位示例（单轮）：“只出条件/时限类事实题，问法贴近学生口语”；
  - 占位示例（多轮）：“围绕转专业的申请条件与材料，生成连续追问”；
  - 500 字上限并显示字数；清空即用默认出题口径；
- 多轮时额外显示：生成条数、每例轮数（2~4，默认 3）、窗口大小（2~3，默认 3）；
- 文案提示：多轮生成按窗口出题，耗时更长，标准答案严格取自原文；自定义提示词只影响选题与问法，不会放宽原文校验。

### 7.4 现有 15 条多轮题

- 保持原样，不自动补答案；
- 列表显示“多轮 N 轮”；
- 编辑弹窗可逐轮补 `ground_truth`；
- 未补答案的轮次：
  - 多轮评测仍算结构指标；
  - RAGAS 只算忠实度/答案相关性；
  - 报告明确写“上下文指标只覆盖有标准答案的轮次”。

### 7.5 测试

新增 `backend/tests/unit/test_generate_multi_turn.py`：

- 窗口构建：相邻、跨页、chunk 重叠去重；
- 校验：接受合法 2~4 轮；
- 标准答案：单句原文通过；2~3 个原文分句拼接通过；改写/概括拒绝；一句原文+一句改写拒绝；数字不在原文拒绝；只有“可以申请”这类短泛化句拒绝；实质分句超过 3 句拒绝；带“此外”这类原文没有的连接词拒绝（严格版）；
- 拒绝：<2 轮、首轮带代词、答案实质分句不是窗口原文、答案重复、问题重复；
- 后续轮无代词但答案合法的用例**仍入库**（不硬拦）；
- 生成的 `question` 等于第一轮问题，`ground_truth` 为 `NULL`；
- 自定义提示词：会出现在最终 Prompt 的“管理员额外要求”段；硬规则仍然存在；
- 自定义提示词要求编造原文之外的内容 → 校验拒绝，不入库；
- `instruction` 为空 → 使用默认出题口径，不出现空段落；
- 单轮生成走同一套逐句校验：多句拼接通过、改写拒绝、数字不符拒绝。

扩展 `backend/tests/integration/test_eval_sets_crud.py`：

- 创建多轮题：`turn_count` 正确、`question` 同步第一轮；
- 编辑 `turns`：更新后 `question` 同步；
- 单轮题传 `turns` → 400/422；
- 多轮题少于 2 轮 → 400/422；
- 列表不返回整段 `turns`，详情返回。

扩展 `backend/tests/integration/test_eval_cases.py`：

- 生成多轮模式：入库 `case_type`、`turns`、`source_snippet` 正确；
- 校验失败的窗口不写入；
- `instruction` 正确透传到 Prompt，并写入 `note`；
- 超过 500 字返回 422；
- `EvalGenerateCaseOut.turn_count` 正确。

扩展 `backend/tests/integration/test_eval_set_io.py`（多轮往返）：

- 导出多轮题 → 导入 → `turns`、`case_type`、`ground_truth=NULL` 原样恢复；
- 导入不会因为用例级 `ground_truth` 为空而报错。

扩展 `backend/tests/unit/test_cite.py` / `test_citation_support.py`：

- 部分支持：句子引用 `[1][2]`，`[1]` 支持、`[2]` 不支持 → `unsupported_markers` 含 2，`unsupported_citations` 为空；
- 全部不支持 → 句子进入 `unsupported_citations`。

前端标注回归（`frontend/web/scripts/annotation_probe/run.ts`）：

- `unsupported` 句子置灰；
- `unsupported_markers` 只影响角标，不影响整句；
- 部分支持场景：句子不置灰，但错编号角标有 `unsupported` class。

---

## 8. 改动范围汇总

| 层 | 文件 / 变更 |
|---|---|
| 迁移 | `005_citation_and_dialogue_mode.sql`：`messages.verify_report`、`eval_runs.dialogue_mode` |
| 后端-问答 | `graph/nodes/citation_support.py`（新）、`graph/nodes/cite.py`、`schemas/chat.py`、`services/conversation_service.py`、`services/chat_service.py`、`config/prompts/generate.txt` |
| 后端-评测 | `eval/runner.py`、`eval/report.py`、`eval/generate.py`、`eval/sets.py`、`schemas/eval.py`、`api/eval.py` |
| 前端 | `api/types.ts`、`api/eval.ts`、`api/schema.d.ts`（重新生成）、`markdown/uncitedAnnotation.ts`、`markdown/MarkdownAnswer.tsx`、`views/chat/ChatPage.tsx`、`views/admin/eval/EvalRunsPage.tsx`、`views/admin/eval/ReportDialog.tsx`、`views/admin/eval/EvalSetsPage.tsx`、`views/admin/eval/EvalAblationPage.tsx`、`index.css` |
| 测试 | `tests/unit/test_citation_support.py`（新）、`test_cite.py`、`test_prompts.py`、`test_generate_multi_turn.py`（新）、`tests/integration/test_eval_runner.py`、`test_eval_api.py`、`test_eval_generate.py`、`test_eval_sets_crud.py`、`test_eval_cases.py`、`test_eval_set_io.py`、`test_eval_compare.py`、`test_conversations_api.py`、`frontend/web/scripts/annotation_probe/run.ts` |
| 生成物 | 后端 `openapi.json`、前端 `schema.d.ts`（`npm run gen:api`） |

---

## 9. 错误处理与边界

| 场景 | 处理 |
|---|---|
| 支持度检查误标 | 保守阈值；只提示不阻断；阈值可用标注样本校准 |
| 支持度检查漏标（改写/同义） | 第一版接受；后续可加向量语义或离线判官 |
| 历史消息没有 `verify_report` | 不标注，与改造前一致 |
| 灰标偏移对不上 | 与现有 uncited 相同：整条消息降级为文字提示，不标位置 |
| 多轮题没有逐轮答案 | 该轮只算生成侧 RAGAS；报告标注分母 |
| RAGAS `reference=None` | 上下文两项为 `null`；不写入均值分母 |
| 多轮生成窗口不足 | 允许重叠窗口均匀取起点；不生成假数据 |
| 多轮生成超时 | 返回已生成部分，提示重试 |
| 自定义提示词与原文/硬规则冲突 | 生成结果过不了逐句原文校验 → 跳过该条，返回原因；不入库 |
| 分句组合逻辑不成立 | 逐句校验只能保证“每句为真”，不能保证“组合合理”；Prompt 要求直接回答问题 + 人工抽查 |
| 自定义提示词超长 | 500 字上限，超出 422 |
| 自定义提示词为空 | 按 `None` 处理，使用默认出题口径 |
| 自定义提示词要求改条数/轮数 | 以界面字段为准，提示词里的相关要求无效 |
| 老 run 没有 `dialogue_mode` | 默认 `single`；对比页显示该模式 |
| 单轮模式跑多轮题 | 使用 `turns[0]`，检索命中用 `turns[0].expected_document` |
| 多轮题 `turns` 被清空 | 拒绝（至少 2 轮） |
| 多轮创建不传 `question` | 由 `turns[0].question` 派生 |
| 多轮 PATCH 单独改 `question` | 400，提示改 `turns` |
| 多轮列表搜索后续轮 | `turns::text ILIKE`，不只搜首问 |
| 多轮源片段跨页 | 文本高亮；PDF 框选按 `source_page`（首块页码） |
| 评测不做滚动压缩 | 2~4 轮可控；作为与线上的已知差异写明 |
| 对比表混排单轮/多轮 | 显示模式，并提示“按题/按轮，分母不同” |
| 部分引用错误 | 错编号角标提示；整句只在**全部**被引片段都不支持时置灰 |
| 手工答案不在原文 | 只提示，不硬拦（已确认） |

---

## 10. 实施顺序

### Phase 1：引用治理（独立可上线）

1. 应用迁移 `005`（同时加 `messages.verify_report` 与 `eval_runs.dialogue_mode`；后者本期先不使用，避免 Phase 2 再走一次迁移链）；
2. `citation_support.py` + `cite.py` + schemas；
3. `generate.txt` 第 6 条 + Prompt 测试；
4. 持久化与历史会话读取；
5. 前端灰标、角标提示与汇总；
6. 单测 + 事故复现测试。

**验收**：把 13:13 那条回答原样输入支持度检查，必须产出 1 条 `unsupported_citations`；支持句不误标；部分支持时只有错编号角标提示、整句不置灰；刷新会话后灰标仍在。

### Phase 2：批量评测单/多轮

1. 使用 `005` 已加的 `eval_runs.dialogue_mode` 列（无需再迁移）；
2. API / runner / report / schemas；逐轮回放时补 `clarify_chain` / `clarify_total`；
3. 逐轮 RAGAS 的 `sample_map` 回填、`ragas_scored_turns`、报告分母；
4. 前端下拉、任务表、报告；对比页增加模式列；
5. 用现有 15 条多轮题验证逐轮回放与结构指标；
6. 集成测试。

**验收**：同一评测集分别跑单轮/多轮，任务表与对比表显示模式；多轮报告出现逐轮结构指标；RAGAS 分母按轮次显示；现有 15 条题不报错。

### Phase 3：多轮用例生成 / 手工录入

1. 手工录入 API + 详情接口 + 校验；多轮的 `question` 由 `turns[0]` 派生；
2. 窗口构建 + 多轮生成 Prompt + 单轮/多轮共用的逐句原文校验；
3. 生成提示词：`instruction` 接口字段、Prompt 拼接、`note` 记录；
4. 前端录入/生成弹窗（含提示词文本框、轮次表）；
5. `case_source` 返回逐轮答案与高亮；多轮导出/导入 round-trip；
6. 用相邻 2~3 chunk 生成一批用例，人工抽查逐句原文；
7. 测试。

**验收**：手工可建/改多轮题；生成的多轮题至少 2 轮，每轮答案都是窗口原文；编辑现有 15 条题可补答案；“核对原文”能看到每轮答案；多轮评测的四项 RAGAS 能覆盖有答案的轮次。

---

## 11. 风险与取舍

| 风险 | 取舍 |
|---|---|
| 字面支持度会漏掉“换了说法的正确引用” | 第一版接受；只提示，不阻断；阈值与规则后续校准 |
| `messages.verify_report` 增加存储 | 每条助手消息多几 KB；换取刷新后仍能看到灰标 |
| 逐轮 RAGAS 成本上升 | 只送 knowledge 且未拒答的轮次；整批一次算；报告明确分母 |
| 2~3 chunk 窗口可能带入无关内容 | Prompt 限定“每个实质分句必须逐字来自窗口”；校验不过就跳过 |
| 逐句拼接仍可能降低生成成功率 | 连接词、超过 3 句、跨窗口分句都会被拒；先跑严格版，通过率低时再加连接词白名单或放宽句数 |
| 分句组合逻辑不成立 | 逐句校验管不了；靠 Prompt“直接回答原问题” + 人工抽查 + 窗口限制在 2~3 chunk |
| 现有 15 条题没有逐轮答案 | 不自动补；提供编辑能力；报告如实标注 |
| 单轮模式改为用 `turns[0]` | 行为变化，但让单/多轮对比有意义；现有 15 条题首轮问题不变，暂无实际差异 |
| 自定义提示词诱导模型编造 | 仅管理员可用；基础 Prompt 声明硬规则优先；逐句校验拒绝，不入库 |
| 自定义提示词过长/注入 | 500 字上限；硬规则与输出格式不可覆盖；生成结果仍逐条校验 |
| 部分引用被角标提示，可能显得嘈杂 | 只在确有把握时标；阈值保守；句子级不置灰，避免误伤整句 |
| 单轮/多轮 run 混在对比表 | 显示模式并提示分母不同；不做自动剔除 |
| 评测不覆盖压缩链路 | 2~4 轮可控；报告/文档写明，不冒充线上完全等价 |
| 窗口跨页高亮不完整 | 文本高亮为准；PDF 框选只按首块页码 |
| 多次生成可能产生重复题 | 第一版不去重；生成后人工清理，后续再加同集去重 |

---

## 12. 验收用例（摘要）

1. **事故复现**：答案含“专科起点本科的毕（结）业证书中注明……[1][2]”，证据是《02》→ `unsupported_citations` 含该句；前端琥珀标；刷新后仍显示。
2. **支持句不误标**：同一句引用《06》p12 → 不标记。
3. **Prompt 断言**：`generate.txt` 含“历史中的事实”“不得挂本轮”，测试通过。
4. **单/多轮对比**：同一评测集跑单轮和多轮；单轮只跑第一问；多轮逐轮回放；报告分别显示“题有分/轮有分”。
5. **逐轮 RAGAS**：knowledge 且未拒答的轮次都产生样本；chat/clarify/拒答不产生；有 `reference` 的轮四项齐全，无 `reference` 的轮上下文两项为 `null`。
6. **多轮生成**：用相邻 2~3 chunk 生成，至少 2 轮；每轮 `ground_truth` 的每个实质分句都能在窗口文本中逐字找到；允许 1~3 个原文分句拼接；改写、数字不符、超过 3 句被拒绝；首轮无代词，后续轮按 Prompt 尽量使用指代（不硬性要求）。
7. **手工录入**：可创建/编辑多轮题；少于 2 轮被拒；答案为空只提示不硬拦；列表显示轮数，详情返回 turns。
8. **现有 15 条题**：不修改也能加载、编辑、跑评测；多轮模式产出结构指标；未补答案的轮次上下文 RAGAS 为空且报告说明原因。
9. **生成提示词**：输入“只出条件/时限类题，问法口语化”，生成结果体现该要求；`note` 记录该提示词；若提示词要求编造原文没有的答案，生成被逐句原文校验拒绝、不入库。
10. **部分引用错误**：句子引用 `[1][2]`，`[2]` 不支持 → 只有 `[2]` 角标提示，整句不置灰；`[1][2]` 都不支持 → 整句置灰。
11. **对比页**：单轮/多轮 run 都进对比表，行上显示模式；混排时有“分母不同”提示。
12. **多轮往返**：多轮题导出再导入，`turns`、`case_type`、`ground_truth=NULL` 原样恢复；前端标注 probe 脚本覆盖 `unsupported` 与部分支持场景。
13. **单轮/多轮校验一致**：单轮生成也能接受“多个原文分句拼接”的答案；改写仍被拒。

> 详细度量方法、样本量与阈值见第 14 节。

---

## 13. 附录：事故证据与实测

### 13.1 原句出处

- 《04 普通高等教育本科学生学籍管理规定（2025修订）》PDF 第 15 页，第四十五条：
  > 专科起点本科的毕（结）业证书中注明“专↵科起点本科学习”。
- 《06 本科生管理规定（桂电学2019-24号）》PDF 第 12 页，第三十五条：同一句。

### 13.2 13:13 本轮引用映射

| 标记 | 文件 | 页码 | 是否含该句 |
|---|---|---|---|
| [1] | 02_学位授予文件 | p2 | 否（就挂在“专科起点…”这句上） |
| [2] | 02_学位授予文件 | p3 | 否（就挂在“专科起点…”这句上） |
| [5] | 12_第二课堂成绩单制度实施办法 | p12 | 否（末尾“二课分未找到”那句的编号） |

### 13.3 RAGAS `reference=None` 实测

```text
样本 1（有 reference）: faithfulness=1.0, answer_relevancy=0.7163,
                         context_precision=1.0, context_recall=1.0
样本 2（reference=None）: faithfulness=1.0, answer_relevancy=0.6905,
                         context_precision=null, context_recall=null
```

结论：没有逐轮标准答案时，仍可送 RAGAS，但只有生成侧两项有分。

### 13.4 逐句原文拼接实测（方案 A）

窗口材料（退役复学）：

> 退役复学学生可以申请免修课程。免修成绩按85分记录。退役复学后所修课程成绩可乘以1.5，最高不超过85分。学校应当审核申请材料。

| 答案 | 结果 | 原因 |
|---|---|---|
| 单句原文 | 通过 | 1 个实质分句命中，覆盖率 0.93 |
| 三句原文拼接 | 通过 | 3 个实质分句全部命中，覆盖率 0.94 |
| 改写概括 | 拒绝 | 分句不是原文 |
| 一句原文 + 一句改写 | 拒绝 | 所有实质分句必须命中 |
| 带“此外，”的原文 | 拒绝（严格版） | 多了原文没有的连接词 |
| 只有“可以申请” | 拒绝 | 没有实质分句 |
| 数字改成 90 分 | 拒绝 | 数字不在原文 |
| 两个真但组合可疑 | 通过 | **规则管不了逻辑组合** |

结论：方案 A 能覆盖“多个原文分句回答一个复杂问题”，同时继续拒绝改写、数字编造和短泛化句；逻辑组合仍需 Prompt + 人工抽查。

---

## 14. 验收标准与度量方法

### 14.0 通用原则

- **先冻结样本，再看结果**：验收集在评测前固定并版本化；看到结果后不得临时换题、换标注。
- **判官独立**：生成用例用模型 A，判“是否合格”不能再用同一个模型自评；至少要用**人工抽查 + 另一个判官模型**，争议由人复核。
- **样本量按阈值定**：见 14.8；观测值贴近阈值时扩大样本，不靠一次幸运结果下结论。
- **不达标不发布**：功能可以灰度，但第 14.1~14.3 的指标是准入门槛，不是“观察项”。
- **“LLM 生成的评测答案”指自动生成用例的 `ground_truth`**，不是系统在评测里的答题结果；系统答题准确率另列为产品指标，不作为本方案准入条件。

---

### 14.1 生成用例答案准确率 ≥ 90%

**为什么不能只看“答案来自原文”**：逐句校验已经保证标准答案的每个实质分句都来自材料，所以“题材正确性”按构造是 100%。这里要量的是**这条用例是否是一个合格的问题-答案对**。

**合格判据（四条全过才算正确）**

1. 答案回答了该轮问题，不是答非所问；
2. 答案覆盖了问题的关键点，不是只答一半；
3. 没有多余、误导或与问题无关的信息；
4. 多轮题的指代/省略在上下文中成立（“那/它/这个”能找到先行词）。

**样本与阈值**

| 项 | 要求 |
|---|---|
| 样本 | 从**通过校验且已入库**的生成用例中随机抽 **100 条**，按文档/单轮/多轮分层 |
| 判官 | 人工 + 独立判官模型；至少 30 条人工复核 |
| 阈值 | 正确 ≥ **90/100** |
| 稳健性 | 若点估计在 90~92，扩到 200 条复测；要求 Wilson 95% 下限 ≥85% |
| 单轮 | 另抽 50 条单轮生成用例，同样口径 ≥90% |

**不纳入这个分母的**

- 生成通过率（被校验接受的条数 / 请求条数）：单独记录，作为效率指标；
- 手工录入用例：人工负责，不做自动准确率门槛。

**生成通过率软目标**：单轮 ≥60%、多轮 ≥50%。低于 30% 说明 Prompt/窗口/校验过严，需要先调整，不直接放量。

---

### 14.2 引用支持度校验：误判率 < 5%

“误判率”必须拆成两个方向，否则“什么都不标”也能拿 0% 误判。

| 指标 | 定义 | 阈值 |
|---|---|---|
| **误报率 FPR** | 实际有字面依据的句子，被判成“不支持”的比例 | **< 5%** |
| **漏报率 FNR** | 实际没有字面依据的句子，没有被标出来的比例 | **< 10%** |
| **事故召回** | 13:13 那条“专科起点…”引用《02》的句子 | **必须 100% 标出** |
| **部分引用正确性** | `[1]` 支持、`[2]` 不支持时，只标 `[2]` 角标、不置灰整句 | **100% 正确** |

**标注集（冻结，200 条）**

- 100 条**有字面依据**：直接引用、多分句拼接、表格行、带数字的条款；
- 100 条**无字面依据**：引错文档、数字不符、上一轮残留事实、无关片段；
- 其中包含 20 条“部分支持”的多编号句子；
- 标注口径是**字面支持**，不是语义支持：换同义词、改写但意思对，仍算“无字面依据”（这是本校验的已知边界，不计入 FPR）。

**样本与阈值**

| 项 | 要求 |
|---|---|
| 标注集 | 200 条，FPR ≤4/100，FNR ≤9/100 |
| 真实日志 | 从 `qa_logs` 随机抽 100 条带引用回答，人工标注后同样阈值 |
| 统计稳健性 | 观测值贴近 5% 时扩到 300~400 条；报告 Wilson 95% 区间 |
| 性能 | 单次回答的支持度检查 p95 < **50ms**，不新增模型调用 |

**不通过时的动作**

- FPR 超标：提高阈值/增加“原文连续片段”快速通道；线上先只保留角标提示，不做整句置灰；
- FNR 超标：降低阈值、加强数字/专名规则；必须保证事故句被标出；
- 两者都差：回到标注集看错例，优先修规则，不直接上线“全标”或“全不标”。

---

### 14.3 Prompt 硬约束效果达到预期

Prompt 是提醒，不是强制；效果用 **A/B** 量，不靠“读起来觉得更严了”。

**测试集（50 段多轮对话）**

- 每段 2~4 轮；
- 前几轮引入一个事实（来自历史答案或另一份文档）；
- 最后一轮的检索证据**故意不含**该事实，或含冲突事实；
- 正确答案应当不提该事实，或明确写“上一轮回答中的内容”；
- 包含 13:13 这段事故对话。

**A/B 设计**

- 同一测试集、同一模型、同一 temperature、同一检索配置；
- A 组：旧 Prompt；B 组：新 Prompt；
- 判官：人工 + 独立判官模型，按同一 rubric。

**指标与阈值**

| 指标 | 定义 | 阈值 |
|---|---|---|
| 历史事实残留率 | 最终答案包含“本轮证据不支持的历史事实”的比例 | B 组 **< 10%**，且相对 A 组下降 **≥ 50%** |
| 显式归因率 | 提及历史事实时，明确标注“上一轮回答中的内容”的比例 | **≥ 80%** |
| 答案质量回退 | B 组 RAGAS 忠实度/相关性相对 A 组 | 下降 **≤ 0.03** |
| 拒答率回退 | B 组拒答率相对 A 组 | 上升 **≤ 5 个百分点** |
| 兜底捕获 | 逃过 Prompt 的残留事实，被支持度校验标出的比例 | **100%**（在测试集上） |

**解释**：Prompt 单独不达标不算致命，只要支持度校验能 100% 兜住；但两者都不达标，必须回到 Prompt/检索修复，不能发布。

---

### 14.4 功能验收

以下每一项都必须有可复现的验证记录（测试或手工步骤）：

1. **事故复现**：13:13 原答案 → `unsupported_citations` 含该句；前端琥珀标；刷新后仍显示；
2. **部分引用**：`[1]` 支持、`[2]` 不支持 → 只标 `[2]` 角标，整句不置灰；两个都不支持 → 整句置灰；
3. **支持句不误标**：抽 20 条直接引用句 → 0 条被标；
4. **单/多轮评测**：同一评测集分别跑单轮/多轮；单轮只跑 `turns[0]`；多轮逐轮回放；报告显示“题有分/轮有分”；
5. **逐轮 RAGAS**：knowledge 且未拒答的轮次都送；chat/clarify/拒答不送；有/无 `reference` 的分母正确；
6. **报告与对比页**：任务表、报告、对比表都显示 `dialogue_mode`；混排有“分母不同”提示；
7. **多轮录入**：创建/编辑/删除多轮题；少于 2 轮被拒；`question` 由首轮派生；列表搜索能搜到后续轮；
8. **多轮生成**：相邻 2~3 chunk；1~3 个原文分句；改写/数字不符/超 3 句被拒；`instruction` 透传并写 `note`；
9. **核对原文**：多轮题返回逐轮答案与高亮；跨页时文本高亮可用；
10. **导出/导入**：多轮题 round-trip 后 `turns`/`case_type`/`ground_truth=NULL` 不变；
11. **现有 15 条多轮题**：不修改可加载、编辑、跑评测；单轮模式行为不变；多轮模式产出结构指标；
12. **迁移幂等**：连续执行两次 `init-db`，不报错，`dialogue_mode` 的 CHECK 约束存在；
13. **前端构建**：`npm run build` 通过；`annotation_probe` 覆盖 `unsupported` / 部分支持场景；
14. **文档**：`docs/评测与ragas.md` 更新逐轮 RAGAS、分母、未跑压缩的说明。

---

### 14.5 性能与稳定性

| 项 | 阈值 |
|---|---|
| 支持度检查 | p95 < 50ms/回答；不新增模型调用 |
| 问答整体延迟 | 相对改造前 p95 增幅 < 5% 或 < 100ms（取更宽者） |
| 多轮评测 | 15 条 × 3 轮在 **15 分钟**内跑完；无整轮超时 |
| 多轮生成 | 5 条 × 3 轮在 **180 秒**内返回或返回部分结果；无 HTTP 5xx |
| RAGAS | 失败率不高于改造前基线；单批超时能被报告说明 |
| 并发 | 同一时间只允许一轮评测的约束不变 |

---

### 14.6 回归与兼容

| 项 | 阈值 |
|---|---|
| 单轮评测回归 | 固定题库四项 RAGAS 相对改造前波动 ≤ **±0.02** |
| 现有 15 条多轮题 | 单轮模式问题/检索结果不变（首轮问题相同） |
| 老 run | 没有 `dialogue_mode` 的 run 默认 `single`，列表/报告/对比页不报错 |
| 老消息 | 没有 `verify_report` 的消息不标注，不报错 |
| 导出/导入 | 现有单轮题 round-trip 不变；多轮题新增 round-trip 通过 |
| 前端 | 类型重新生成；`npm run build` 通过；历史会话能显示灰标 |
| 迁移 | 002~005 全部重复执行无副作用 |

---

### 14.7 安全与可观测性

| 项 | 要求 |
|---|---|
| 生成权限 | 仅 admin；`instruction` ≤500 字；无效 `turns/window` 422 |
| 提示词注入 | 自定义提示词不能改变输出格式/原文校验；编造内容不入库 |
| 数据权限 | 多轮评测遵守 `include_restricted`/角色；`unauthorized_hits = 0` |
| SQL | `turns::text ILIKE` 等查询全部参数化，不拼用户输入 |
| 指标可查 | `qa_logs.verify_report` 含 `unsupported_citations`/`unsupported_markers`；评测报告含 `citation_support_rate` |
| 日志 | 多轮评测日志带 `run_id/case_id/turn`；生成日志带文档与是否使用自定义提示词 |
| 审计 | 生成动作建议记审计（P2，不阻塞上线） |

---

### 14.8 样本量与统计口径

- **90% 准确率**：100 条起；若点估计 90~92%，扩到 200 条；报告 Wilson 95% 下限；
- **5% 误报率**：100 条有依据句子起；0~1 条误报才能较稳地声明 <5%；贴近阈值时扩到 300~400 条；
- **Prompt A/B**：50 段对话起；若 A/B 差异接近阈值，扩到 100 段；
- **人工一致性**：双人标注 ≥20% 样本，Cohen's kappa ≥0.7；分歧由第三人复核；
- **冻结**：验收集版本化，改动后重新跑基线，不与旧结果混比。

---

### 14.9 不达标时的处理

| 不达标项 | 处理 |
|---|---|
| 生成答案准确率 <90% | 按四类失败拆解（答非所问/答一半/多余信息/指代不成立）；先改 Prompt 与窗口，再重跑 |
| FPR ≥5% | 提高阈值、加“连续原文快速通道”；线上先只角标提示，不整句置灰 |
| FNR ≥10% | 降低阈值、加数字/专名规则；事故句必须命中 |
| Prompt 效果不足 | 加强 Prompt；若仍不足，依赖支持度校验兜底；两者都失败则阻塞发布 |
| 性能回退 | 缓存证据的归一化单元；限制句子数；profile 后再放量 |
| 评测过慢 | 降轮数/样本数，UI 显示预计样本数；改为夜间跑 |
| 兼容性回退 | 回滚对应 Phase；Phase 1/2/3 可独立回退 |

---

### 14.10 落地前置条件（不满足就无法验收）

| 前置条件 | 说明 |
|---|---|
| **冻结验收集** | 200 条引用标注集、100 条生成用例集、50 段 Prompt A/B 对话，落到 `test_data/` 或评测集，版本化 |
| **标注负责人** | 明确谁标、谁复核、争议谁裁决；预计 2~3 人日 |
| **基线数据** | 改造前先跑旧 Prompt / 旧校验，记录同一批样本的基线，否则 A/B 和 ±0.02 回归没有参照 |
| **判官独立性** | 生成与评判不能用同一个模型自评；至少 30% 人工复核 |
| **CI 分工** | 单测/集成测试每次 PR；引用 FPR/FNR、生成准确率、Prompt A/B 走夜间或发版前验收 |
| **预算** | 验收跑要调用生成模型、RAGAS 判官；预留调用次数与时间，避免中途停 |
| **回滚方案** | Phase 1/2/3 独立开关；引用校验异常时先只保留角标提示，不做整句置灰 |
| **运行手册** | 写清阈值怎么调、样本怎么扩、失败怎么回滚、报告怎么读 |
