# 评测与 ragas 接入（M5）

> 本文记录 **ragas 怎么接进来的、为什么是这个形状**，以及踩过的坑。
> 全部结论都是 2026-10-06 在这台机器上实测得到的，不是照文档推的。

---

## 一句话结论

**ragas 装在隔离环境 `.venv-ragas` 里，主环境一个包都不动。**

主环境跑 RAG 链路、收集样本；样本交给 `.venv-ragas` 的解释器起的子进程算指标；
分数收回来落 `eval_case_results.metrics`，聚合后进 `eval_runs.metrics` → 前端评测页。

---

## 为什么不装进主环境（两条硬阻断，都实测过）

### 1) ragas 0.4.3 与 langchain-community 0.4.x 不兼容 —— **连 import 都过不去**

```
File ".../ragas/llms/base.py", line 12, in <module>
    from langchain_community.chat_models.vertexai import ChatVertexAI
ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'
```

`vertexai` 这个模块在 langchain-community 0.4.x 里已被删除。**在全新空 venv 里
单独装 ragas 也是同一个错** —— 所以不是我们钉死版本造成的，是上游不兼容。

### 2) 装进主环境会让 **`import sentence_transformers` 直接段错误**

Windows fatal exception: **access violation**，崩在 `pyarrow/__init__.py` 初始化，
调用链是 `sentence_transformers → sklearn → pandas → pandas.compat → pyarrow`。
而 **reranker 正是靠 sentence-transformers 加载的** —— 等于检索质量的地基被抽掉。

**受控实验（两腿只差一个变量）**：

| 状态 | `import sentence_transformers` |
|---|---|
| `uv add ragas "huggingface-hub<2"` 之后 | **段错误，退出码 139** |
| `git checkout` 回滚依赖 + `uv sync` 之后 | **正常，退出码 0** |

> 根因**没有定位到具体是哪个传递依赖**（pyarrow/pandas/sklearn 版本都没变，
> 单独 `import pyarrow` 也是好的）。如实记录：知道"装了什么会坏、怎么躲开"，
> 但没查到"为什么坏"。隔离环境规避了它，不影响交付。

**顺带一个发现**：`uv pip list` 报的 pyarrow 版本与 venv 里真实的不是一回事
（报 23.0.1、实际 24.0.0）—— 判断版本别用 `uv run pip list`，用
`uv run python -c "import pyarrow; print(pyarrow.__version__)"`。

---

## 接线上的另外三个坑（都在 runner 里注释了）

| # | 坑 | 症状 | 修法 |
|---|---|---|---|
| 1 | ragas 要 `langchain-community<0.4` | `ModuleNotFoundError: vertexai` | 隔离环境里钉 `0.3.31` |
| 2 | **判官必须关思考** | `IncompleteOutputException: 输出不完整` | `llm_factory(..., reasoning_effort="none")` |
| 3 | 嵌入要用 **langchain 式**的 | `AttributeError: 'OpenAIEmbeddings' object has no attribute 'embed_query'` | 用 `langchain_openai.OpenAIEmbeddings`，不要用 `ragas.embeddings.OpenAIEmbeddings`（那版只有 `embed_text`） |

**第 2 条最隐蔽**，实测证据（同一提示词、`max_tokens=1024`）：

| 调用 | finish_reason | completion_tokens | reasoning 字数 | **content 字数** |
|---|---|---|---|---|
| 默认（思考模式） | `length`（截断） | 1024 打满 | **1818** | **0** |
| `reasoning_effort="none"` | `stop` | 6 | 0 | 12 |

思考模式把整个输出预算烧在 `reasoning_content` 上，正文一个字都没有 ——
这与 §1.0 里主模型必须关思考是同一条口径，**对 ragas 的判官调用同样成立**。

---

## 建环境 / 怎么验

```bash
# 建（仓库根，两条命令）
uv venv .venv-ragas
uv pip install --python .venv-ragas/Scripts/python.exe -r backend/tools/requirements-ragas.txt
#   Linux/macOS：把 Scripts/python.exe 换成 bin/python

# 验（主环境侧，端到端：起子进程 → ragas → 收分）
cd backend && uv run python -m app.services.ragas_service
```

期望看到四指标出数，例如：

```json
{"ok": true, "ms": 20024,
 "rows": [{"faithfulness": 1.0, "answer_relevancy": 0.712,
           "context_precision": 1.0, "context_recall": 1.0}],
 "means": {...}, "errors": [], "available": true}
```

隔离环境不存在时**不报错**，返回 `available: false` —— 与 `stats/retrieval`
对 Prometheus 的处理同一口径（M4-D5）。

---

## 落点（指标怎么到前端）

```
主环境：真实图跑一遍每题 → (question, answer, retrieved_contexts, ground_truth)
   ↓  backend/app/services/ragas_service.py（起子进程、喂 JSON、收 JSON）
.venv-ragas：backend/tools/ragas_runner.py（ragas 四指标）
   ↓  分数
eval_case_results.metrics（逐题） → eval_runs.metrics（整轮聚合）
   ↓  GET /api/admin/eval/compare（服务端派生 config_label + 对齐矩阵）
前端评测页（触发 / 历史 / 消融对比表）
```

---

## 四项均值的口径（2026-10-16 修正）

**问「这几项均值算在几道题上」时，答案不是「用例数」—— 分母有两层，都要看得见。**

① **拒答的题不送 ragas**（门槛在 `app/eval/runner.py`）。拒答在 faithfulness /
answer_relevancy 上按构造得 0，送进去等于拿**拒答率**冒充**答案质量** ——
实测「第二课堂评分表专项」那轮：answer_relevancy 10 题 0.5494、
只看 6 道作答题 0.9157，差的全是 4 道拒答题。拒答率仍由
`refused_count` / `refusal_rate` 单独报，不混进这四项。

② **单题判官失败的行不算「有分」**。`tools/ragas_runner.py` 用
`raise_exceptions=False`，失败的那题**不删行**：NaN 全部换成 `None` 留一行原样返回，
只有某项**整列**都没算出来才写进 `errors`。所以拿「返回的行数」当分母是错的 ——
评审实测 `506f109f`（90 题）：63 行带分、但 19 行四项全 `None`、
3 行部分 `None`，**每项均值其实只算在 42–43 行上**（63 是那个被误当分母的数）。

落点：整轮 `metrics.ragas_scored_cases` 数的是**真的算出了分的行数**
（至少一项非 `None`）；报告里**每张卡各自带 `scored_cases`**（该指标的
非 `None` 计数，各指标可以不同）；结论句分数不一致时报范围
（`（各指标 42–43/90 题有分）`）。老轮次没有这些键，照旧不念分母。

---

## 已知问题（如实记录）

| # | 问题 | 影响 | 处置 |
|---|---|---|---|
| 1 | **`answer_relevancy` 有跑间随机性**：同一条样本两次跑出 0.8901 与 0.712 | 消融表里这一列的差值可能小于噪声 | 报告里**标注该列不可用于小于 ~0.15 的差值判断**；需要更稳时给判官固定 seed 或调 n |
| 2 | ragas 告警 `LLM returned 1 generations instead of requested 3` | AnswerRelevancy 默认想采 3 个生成取平均，我们的模型每次只回 1 条 | 已在 `errors` 之外单独可见；不影响其它三项 |
| 3 | 段错误根因未定位到具体包 | 只影响"能不能装进主环境"这个已放弃的方案 | 不需要再查；隔离方案已规避 |
| 4 | 每题约 20 秒（1 样本 4 指标） | 60–80 题一轮是分钟级；消融 8 行要按小时算 | 用 `evaluate(dataset)` **整批并发**，不要逐题调用 |
