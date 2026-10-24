# 引用支持度与多轮评测：功能验收报告

- 日期：2026-10-24
- 分支：`refactor/campus-rag`
- 设计稿：`docs/superpowers/specs/2026-10-23-citation-support-and-multiturn-eval-design.md`
- 执行计划：`docs/superpowers/plans/2026-10-24-citation-support-and-multiturn-eval.md`

---

## 1. 结论先说

**§14.4 功能验收 14 项已逐项执行，全部通过。**
**§14.1~§14.3 的统计验收（生成准确率、引用 FPR/FNR、Prompt A/B）本次未执行** ——
它们需要冻结标注集、人工复核、独立判官模型和改造前基线，不是自动化测试能替代的。
因此本报告**不宣称**功能已满足发布准入门槛，只证明功能行为符合设计稿。

后端全量测试：**920 passed**（`uv run pytest backend/tests -q`）。
前端构建：**通过**（`npm run build`）。
前端标注回归：**15/15 通过**（`npx tsx scripts/annotation_probe/run.ts`）。

---

## 2. 测试环境

| 项 | 状态 |
|---|---|
| PostgreSQL | Docker `postgres:17-alpine`，健康 |
| 后端 | 新代码另起 `127.0.0.1:8091`（验收脚本用；原 8090 未改动） |
| 语料 | 活库 12 份文档，137 条评测用例，其中 15 条多轮题 |
| LLM | `.env` 已配置 DeepSeek / 阿里云，真实调用 |
| RAGAS | `.venv-ragas` 可用，真实逐轮计算 |
| 迁移 | `init-db` 连续执行两次成功，`eval_runs_dialogue_mode_check` 存在 |

验收脚本：`backend/tests/e2e_acceptance_http.py`（HTTP 端到端，非 pytest 收集）。
输出另存：`logs/e2e_http_acceptance.json`、`logs/e2e_multiturn_eval.json`（本地证据，未入库）。

---

## 3. §14.4 功能验收逐项

### 3.1 事故复现（引用支持度）

用**活库真实 chunk**（不是合成文本）复现 13:13 事故：

- 句子：`专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。`
- 被引片段：`02_学位授予文件` 的两个真实 chunk。
- 结果：`unsupported_citations` 含该句，`unsupported_markers` = `[1, 2]`，
  `citation_support_rate = 0.0`。
- 同一句引用 `06_本科生管理规定` 第 12 页真实 chunk：`unsupported_citations = []`，
  `unsupported_markers = []`，`supported_claims = 1`，`citation_support_rate = 1.0`。
- 落库/刷新：插入带 `verify_report` 的助手消息后，`GET /api/conversations/{id}/messages`
  原样返回该报告；老消息（无该列）返回 `null`。

### 3.2 部分引用

单测 `test_citation_support.py::test_partial_support_flags_only_the_bad_marker`
与 `test_cite.py::test_partial_support_flags_bad_marker_without_greying_sentence`：

- `[1]` 支持、`[2]` 不支持 → `unsupported_markers` 只有 `[2]`；
- `unsupported_citations` 为空（整句不置灰）；
- 前端 probe `部分支持只影响角标` 通过。

### 3.3 支持句不误标

真实语料正确文档场景 `supported_claims = 1`、无任何 unsupported 记录；
PDF 硬换行（`专\n科起点本科学习`）归一化单测通过。

### 3.4 单轮 / 多轮评测

- 单轮模式：多轮题只跑 `turns[0]`，`ground_truth` 用第一轮答案，
  检索命中用 `turns[0].expected_document`（`test_eval_runner.py` 覆盖）。
- 多轮模式：用现有 `mt-mt-01`（3 轮）真实跑图。结果：
  - `turns_total = 3`、`turns_completed = 3`
  - 路由准确率 `1.0`、澄清准确率 `1.0`
  - 检索命中率 `1.0`、消解锚点命中率 `1.0`
  - 引用支持率 `1.0`，不支持引用 `0` 处 / `0` 句
- 历史逐轮累加、`last_route`、`clarify_chain` / `clarify_total`
  由集成测试断言（`test_multi_mode_replays_turns_with_history_and_clarify_counts`）。

### 3.5 逐轮 RAGAS

真实 3 轮评测中：

- 第 1、2 轮 `knowledge` 且未拒答 → 送 RAGAS；
- 第 3 轮拒答 → **不送**；
- `ragas_scored_turns = 2`；
- 没有逐轮标准答案（`reference = None`）→ 忠实度/答案相关性有分，
  上下文精度/召回为 `null`，未进入均值分母。
  实测：`faithfulness = 1.0`、`answer_relevancy = 0.4814`、
  `context_precision = null`、`context_recall = null`。

集成测试 `test_sample_map_fills_each_turn` 另验证了 `sample_map` 把每条样本
回填到正确的 `(case, turn)`，不会互相覆盖。

### 3.6 报告与对比页

HTTP 验收中构造多轮 run 后：

- `GET /runs/{id}`：`dialogue_mode = "multi"`、`report.multi_turn.turns_total = 2`、
  `report.ragas_scored_turns = 2`、`cases[0].turns` 长度 2；
- `GET /compare`：行上 `dialogue_mode = "multi"`，
  指标全集含 `citation_support_rate`、`unsupported_markers`、
  `unsupported_citations`、`ragas_scored_turns`、`turns_total`。
- 前端报告弹窗与对比页已按这些字段实现，构建通过。
- 批量评测页在选好评测集后显示预计 RAGAS 样本上限
  （单轮按题、多轮按「多轮题 turns 长度 + 普通题 1 轮」），
  并注明只送 knowledge 且未拒答的轮次/题。

### 3.7 多轮录入

HTTP 验收：

- 创建多轮题 → `turn_count = 2`，`question` 由 `turns[0]` 派生；
- `GET /cases/{id}` 返回完整 `turns`；
- `PATCH turns` 后 `question` 同步为新的第一轮问题；
- 列表搜索后续轮问题（`q=材料呢`）能命中；
- 少于 2 轮 → 422；单轮题带 `turns` → 422 / 400；多轮题单独改 `question` → 400。

### 3.8 多轮生成

真实 LLM、真实向量库、HTTP 调用：

- 请求 3 条、每例 2 轮、窗口 2；
- 实际生成 **3 条，每条 3 轮**，`failed = 0`，无超时；
- 另一次独立脚本生成中，逐轮 `ground_truth` 的每个实质分句都在
  `source_snippet` 中逐字命中（`gt_verbatim=True`），
  `case_type='multi_turn'`、`question=turns[0]`、`ground_truth=NULL`、
  `note="生成要求：..."` 均已确认；
- 首轮带代词的输出会被拒绝；改写/数字不符/超过 3 句/答案重复由单测覆盖。

### 3.9 核对原文

HTTP 验收：`GET /cases/{id}/source` 返回：
- `turns` 两轮；
- `turn_highlights = [[21,28], [29,36]]`，逐轮高亮来自 `turn_highlights`；
- 前端弹窗按轮展示问题/答案，并在片段里标出对应高亮。

### 3.10 导出 / 导入

多轮题 round-trip 测试通过：`turns`、`case_type='multi_turn'`、
`ground_truth=NULL` 原样恢复；单轮题原有往返不变。

### 3.11 现有 15 条多轮题

- 库中 15 条多轮题原样保留，未自动补答案；
- 列表可加载并显示「多轮 N 轮」；
- 详情/编辑接口可用；
- 用其中 `mt-mt-01` 真实跑完多轮评测（见 3.4 / 3.5）。

### 3.12 迁移幂等

`init-db` 连续两次成功；`messages.verify_report` 与
`eval_runs.dialogue_mode` 均存在；`eval_runs_dialogue_mode_check` 存在。

### 3.13 前端构建与标注回归

- `npm run build`：通过；
- `annotation_probe`：15/15 通过，含：
  - `unsupported` 整句置灰；
  - 部分支持只影响角标、不影响整句；
  - `unsupported + 角标` 共用管道顺序正确。

### 3.14 文档

`docs/评测与ragas.md` 已补：单/多轮执行、逐轮 RAGAS、`sample_map`、
分母口径、无 `reference` 时的表现、未跑滚动压缩的已知差异。

---

## 4. 未执行 / 未覆盖（如实说明）

| 项 | 状态 | 原因 |
|---|---|---|
| §14.1 生成用例答案准确率 ≥90% | **未执行** | 需要 100 条冻结样本、人工 + 独立判官模型复核；本次只验证了“逐句原文”构造正确性 |
| §14.2 引用误报率 FPR <5% / 漏报率 FNR <10% | **未执行** | 需要 200 条冻结标注集与真实日志 100 条人工标注 |
| §14.3 Prompt A/B | **未执行** | 需要 50 段多轮测试集、旧 Prompt 基线、独立判官 |
| 15 条多轮题全量跑 | **只跑了 1 条** | 全量 15×3 轮会调用大量模型与 RAGAS；本次用 `mt-mt-01` 真实验证链路，其余 14 条未逐条跑 |
| 浏览器手工点击 | **未执行** | 本次为构建 + 标注 probe + HTTP 验收；未用浏览器逐页点击 |
| 线上延迟 p95 | **未测量** | 支持度检查是纯字面计算，单测级别毫秒；未在真实问答链路做 p95 压测 |

这些项是设计稿 §14.1~§14.3 的**发布准入门槛**，不是本次功能验收能替代的。
在完成标注集与基线之前，不应把本次结果当作“可发布”的统计证据。

---

## 5. 复现方式

```bash
# 1) 后端全量测试（真实 PostgreSQL）
cd D:\Knowledge_rag_system
docker compose up -d --wait postgres
uv run pytest backend/tests -q

# 2) 迁移幂等
cd backend
uv run python -m app.cli init-db
uv run python -m app.cli init-db

# 3) 前端构建与标注回归
cd ../frontend/web
npm run build
npx tsx scripts/annotation_probe/run.ts

# 4) HTTP 端到端验收（需新代码后端跑在 8091）
cd ../../backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8091
uv run python -m tests.e2e_acceptance_http
```

> Windows 独立脚本驱动真实图时，rerank 必须主线程直跑 `rerank_sync`；
> 走 `asyncio.to_thread(torch/CUDA)` 会命中已知的 SIGSEGV（见项目记忆/
> `docs` 相关记录）。验收脚本里的真实多轮评测已按此方式执行。
