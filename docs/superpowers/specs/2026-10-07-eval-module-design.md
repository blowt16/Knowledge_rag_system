# 评测模块重构 — 设计方案

> **日期**：2026-10-07 ｜ **分支**：`refactor/campus-rag` ｜ **状态**：待负责人审阅
>
> **依据**：负责人提供的参考文档（`评测模块参考`，含 8 张界面参考图）+ 本仓库现状实测。
> **上游**：`docs/校园RAG系统重构方案.md` §3.7.3（管理端接口）、§4.3.1.4（消融对比表）、§5.3（消融实验）、`docs/评测与ragas.md`。
>
> 本文只写「做什么、为什么这么定」。**凡与 §3.7.3 / §4.3.1.4 冲突处，本文明确标注并给出修订理由**——不默默改口径。

---

## 0. 一句话

把评测从「一张全局题库 + 一个消融对比表」改造成**评测集 → 用例 → 按集跑测 → 出报告**四段式，
同时**保留**既有的消融对比能力。做法是在现有三张表上做**纯增量扩展**，不动老字段、不动老接口语义。

---

## 1. 现状（动手前必须知道的六件事）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 题库是**一张全局表** `eval_cases`（库里 90 条），只有 `suite ∈ {full, refusal_calib}` 两档分组，**没有"评测集"实体** | `001_init.sql` §10 |
| 2 | 用例缺三个界面要的字段：来源、参与评测、备注 | 同上 |
| 3 | 用例**没有任何界面增删改入口**，只能跑脚本重灌 | `tools/make_eval_cases.py`、`cli.py seed-eval-cases` |
| 4 | 每轮的**进度不存在**：`eval_runs.status` 只有 4 个取值，没有计数器 | `001_init.sql` §11 |
| 5 | 逐题的 ragas 四项分数**已经存在** `eval_case_results.metrics`（键名 `ragas_<指标>`），只是没有界面读它 | `services/eval_service.py` |
| 6 | `eval_case_results.case_id` 是**无 ON DELETE 的外键** → **现在删不掉任何跑过的用例**（删就违反外键） | `001_init.sql` §12 |

### 1.1 五个必须说清的事实（都是实测确认的，不是推断）

**① 现在「跑全量」实际跑的是纯向量检索，不是线上链路。**

库里的 run 记录：

```
name: 消融-1 纯向量检索   config: {"suite": "full"}
name: 消融-2 +BM25       config: {"bm25": true, "suite": "full"}
name: 校准小集（admin）   config: {"suite": "refusal_calib"}
```

`retrieval/eval_config.py` 的 `switches()` 用「config 字典是否为空」判断"这轮是不是消融运行"。
而 `api/eval.py` 建 run 时**永远会塞一个 `suite` 键**，字典永远非空 → **每一轮都被判成消融运行** →
六个开关全关 = **纯向量检索**。

轮次名字叫「消融-1 纯向量检索」说明这是**有意为之**：它是消融实验的基线。但它同时也是界面上
「跑全量」按钮的行为——**点"跑全量"得到的是基线分，不是线上分**。

**② ragas 不是消融专用的，是"每一轮评测"的标准配置。**

| 入口 | 走 `eval_service.run_eval` | 算 ragas |
|---|---|---|
| `POST /api/admin/eval/run`（界面所有评测） | ✅ | ✅ |
| `tools/run_ablation.py`（打同一个 HTTP 接口） | ✅ | ✅ |
| `cli.py eval-calibration`（CI 的回归 job） | ✅ | ✅ |
| `cli.py eval-multiturn` | ⚠️ **过图**，但停在 retrieve 之前（`_drive_to_retrieve`） | ❌ |
| `cli.py eval-retrieval` / `seed-eval-cases` | ❌ 不过图 | ❌ |

**消融实验没有特殊通道**——它只是「带开关的一轮评测」，和界面点"开始评测"走同一条代码路径。

**④ 前端从来没有过消融开关。** `EvalPage.tsx` 里只有「角色 / 提权 / 跑全量 / 跑校准小集」，
六个开关（BM25/RRF/精排/三种查询扩展）**一个都没有**，`startEvalRun` 也从不传 `config`。
所以**今天前端产不出任何非基线的 run**——消融行只能靠 `run_ablation.py` 脚本打。
§6.4 要补这个 UI，那是**新增工作**，不是"搬过去"。

**⑤ `suite='full'` 实际跑的是全部 90 条，不是 75 条。**

```python
elif suite == "refusal_calib":
    rows = await conn.fetch("SELECT * FROM eval_cases WHERE suite='refusal_calib' ...")
else:                                    # ← suite='full' 落到这里
    rows = await conn.fetch("SELECT * FROM eval_cases ...")     # 全部 90 条
```

库里实测 `full=75 / refusal_calib=15`。**「跑全量」= 90 题**（含那 15 条校准题）。
这条决定了 §5.5 怎么处理 `suite` 路径——按 `suite` 分组迁进两个集会让它变成 75 题。

**③ 一个真 bug**：`services/eval_service.py` 里单题跑挂时

```python
results.append({"case_id": case["id"], "error": f"{type(e).__name__}: {e}"})
```

这个 `error` **写库时被丢掉了**（下面只取 `r.get("metrics")`，而这条记录根本没有 `metrics` 键）。
所以库里所有失败题，失败原因都是空的。新界面的「失败原因」列要能用，得先补上。

---

## 2. 决策清单

以下 24 条是设计过程中逐条确认的，**实现时不得自行改动**。

| # | 决策 | 备注 |
|---|---|---|
| 1 | **不改主流程**：在现有三张表上纯增量扩展 | 否决了"另起一套表"与"推倒重来" |
| 2 | 新增 `eval_sets` 表；`eval_cases` 挂 `set_id` | 一个用例属于**一个**评测集（1:N，非多对多） |
| 3 | 「从点踩沉淀」**不做** | 负责人指定 |
| 4 | 「检索策略」选择器**不做**；但报告头保留「策略：」那行 | 负责人指定 |
| 5 | 「从文档自动生成」**做**，但弹窗去掉「知识库」这一级 | 项目无知识库概念；直接选文档 |
| 6 | 进度用**轮询 1~2 秒**实现（不引入 SSE） | 沿用 §3.7.3「评测不做 SSE」的既定决策 |
| 7 | 后端建**代码模块目录** `backend/app/eval/`，评测代码集中于此 | 含搬入 `eval_config.py` |
| 8 | 后端建**数据目录** `backend/eval_sets/`，存导出的评测集文件 | 增删改用例时**不自动落盘**，导出要有明确动作 |
| 9 | 侧栏新增三个子页：评测集管理 / 批量评测 / 消融对比 | 参考图只有前两个，消融对比是负责人要求保留的 |
| 10 | 用例新增 `source` / `in_eval` / `note` 三字段 | 对应界面「来源/参与评测/备注」 |
| 11 | 用例新增 `source_document_id` / `source_chunk_id` / `source_page` / `source_snippet` | 「核对标准答案」要展示定位与原文 |
| 12 | **`source_snippet` 必须存**：文档重新索引后 chunk 边界会漂 | 见 §8.3 |
| 13 | 报告的综合得分**缺项就显示「—」** | 三项平均冒充综合分比不给分更误导 |
| 14 | 删评测集**连带删它的用例**；**历史 run 不受影响** | run 存 `set_name` 快照 |
| 15 | 「批量评测」跑**线上完整链路**（不是纯向量基线） | 见 §5.4，这条要改 `api/eval.py` 的建 run 逻辑 |
| 16 | 点「开始评测」**只跑选中的评测集**，且只跑 `in_eval=true` 的用例 | 「参与评测」开关因此真正生效 |
| 17 | 「批量评测」顶部保留**角色下拉**（默认学生）；提权 + 消融开关放消融对比页 | 比参考图多一项 |
| 18 | 生成条数**上限 10**、**同步等待**、超时**返回部分结果** | 见 §8.2 |
| 19 | 老 90 条用例的来源一律标「文档生成」 | 它们都不是在界面上手工敲的 |
| 20 | 「失败原因」列**只在真出错时填**，拒答不算失败 | 见 §7.4 |
| 21 | 评测集管理页加**「导出评测集」按钮**，位置在**「删除评测集」右边** | 导出当前选中的评测集为 json；见 §5.1 / §6.2 |
| 22 | **`suite` 老路径完全不碰集合**，保持原 SQL（`suite='full'` 仍跑全部 90 条） | 消融/CI 历史可比、老脚本一行不改；见 §5.5 |
| 23 | 消融对比页**补六个消融开关 UI**（本仓库原本没有这个界面） | 见 §6.4；这是新增工作 |
| 24 | 「看报告」**等评测跑完才可点**；跑完（或失败）**弹一个自动消失的飘窗** | 见 §6.3；用 `@base-ui/react` 自带的 Toast，不加依赖 |

---

## 3. 数据模型

### 3.1 总原则：历史是快照，删配置不动历史

评测的价值就在「三个月前那次跑分是多少」。如果删掉一个用例、历史报告里那行就跟着消失，
评测记录就没法当证据用。**下面所有外键都按这条来。**

### 3.2 新增 `eval_sets`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | |
| `name` | TEXT NOT NULL **UNIQUE** | 界面显示成「售后问题测评（5 条用例）」，括号里的数实时算 |
| `description` | TEXT | 「新建评测集」弹窗里那个文本框 |
| `created_by` | TEXT FK→users(id) | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

### 3.3 `eval_cases` 加 8 列（老字段一律不动，含 `suite`）

| 新列 | 类型 | 说明 |
|---|---|---|
| `set_id` | TEXT FK→eval_sets(id) ON DELETE CASCADE | 归属评测集 |
| `source` | TEXT NOT NULL DEFAULT `'manual'`，CHECK ∈ (manual, generated) | 「来源」列与筛选器 |
| `in_eval` | BOOLEAN NOT NULL DEFAULT TRUE | 「参与评测」开关 |
| `note` | TEXT | 「备注」 |
| `source_document_id` | TEXT FK→documents(id) | 哪份文档 |
| `source_chunk_id` | TEXT | 哪个片段 → 图上「片段 #237」 |
| `source_page` | INTEGER | 第几页 → 图上「第 3 页」 |
| `source_snippet` | TEXT | **那段原文本身** |

> 索引：`idx_eval_cases_set ON eval_cases (set_id)`、`idx_eval_cases_source ON eval_cases (source)`。

> ⚠️ **`case_type` 是 `NOT NULL` 且没有 DEFAULT**（`001_init.sql:222`）。而参考图的
> 「评测用例」弹窗只有 问题/标准答案/参与评测/备注 四个字段，**没有"题目类型"**。
> 所以**服务端建用例时必须自己填一个**，否则第一次手工录入就拿到
> `null value in column "case_type" violates not-null constraint`。
> 取值见 §5.2（手工录入）与 §8.1（自动生成）的字段清单。

### 3.4 `eval_runs` 加 5 列

| 新列 | 说明 |
|---|---|
| `set_id` | TEXT FK→eval_sets(id) **ON DELETE SET NULL** |
| `set_name` | TEXT。评测集名字的**快照**——评测集删了，历史里名字还在 |
| `total_cases` | INTEGER。本轮共几题 |
| `done_cases` | INTEGER NOT NULL DEFAULT 0。已跑完几题 → 界面上 `0/5` |
| `error` | TEXT。失败原因 → 界面「失败原因」列 |

> `set_id` **只写这一列，绝不进 `config`**。塞进 config 会让字典非空 → 被判成消融运行
> → 又跑成纯向量基线，正是 §1.1① 那个坑。

**耗时(ms) 不存列**，用 `finished_at - started_at` 现算——少一个要保持同步的字段。

### 3.5 `eval_case_results` 加 3 列 + 改两个外键

| 改动 | 说明 |
|---|---|
| 加 `question` / `ground_truth` | **快照**：跑的时候把该题的问题与标准答案抄一份进来 |
| 加 `error` | 单题失败原因（**修 §1.1③ 那个 bug**） |
| `case_id` 改为**可空** + 外键改 `ON DELETE SET NULL` | 用例删了，历史那行还在，只是不再指向某个用例 |
| `run_id` 外键改 `ON DELETE CASCADE` | 删一轮 run = 连它的逐题结果一起删。**不改的话 `DELETE /runs/{id}` 会被外键挡住**（`001_init.sql:270` 现在没有 ON DELETE） |

前两条让**历史报告自给自足**（报告不再需要 join `eval_cases`）；第三条解开 §1 第 6 条那个死结；
第四条给 §5.3 新加的「删除」按钮开路。

### 3.6 数据目录 `backend/eval_sets/`

```
backend/eval_sets/
    README.md
    默认题库.json          ← 迁移完成后导出生成，提交进仓库
    拒答校准小集.json       ← 同上
```

**它不是运行时存储，是导出落脚点。** 界面上新建/改用例**只写库，磁盘上不落东西**；
只有手动跑一次导出命令，文件才会被写出来。一个评测集 = **一个 json 文件**（不是目录）。

为什么不让文件当存储：界面要分页/筛选/搜索（文件得全读全解析）、历史报告靠外键引用用例
（文件没有外键）、评测正在跑时改文件行为无法定义、后端多进程同写会打架。

工具：`backend/tools/eval_set_io.py`

```
export <评测集名>     库 → backend/eval_sets/<名字>.json
import <文件>         文件 → 库（按 id 覆盖，可重复跑，幂等）
```

**界面上的「导出评测集」按钮与这个 CLI 共用同一个序列化函数**
（`app/eval/sets.py::serialize_set`）——两边各写一份迟早会漂，导出的文件就对不上了。
区别只在出口：CLI 写进 `backend/eval_sets/`，界面走 HTTP 让浏览器存到本地。

**导出文件的内容**：

```json
{ "version": 1,
  "set": { "name": "...", "description": "..." },
  "exported_at": "2026-10-07T12:00:00Z",
  "cases": [ { "id": "...", "question": "...", "ground_truth": "...",
               "case_type": "factual", "suite": "full",
               "expected_doc_ids": ["..."], "expected_chunk_ids": [],
               "turns": null, "visible_roles": null,
               "expected_route": "knowledge", "should_clarify": 0,
               "source": "generated", "in_eval": true, "note": null,
               "source_document_id": "...", "source_chunk_id": "...",
               "source_page": 3, "source_snippet": "..." } ] }
```

> ⚠️ **字段必须列全，一个都不能省。** 90 条老题里 **15 条多轮题靠 `turns`**、
> 受限题靠 `visible_roles`、A 组指标靠 `expected_route`/`should_clarify`。
> 漏字段 = 这份「题库的 git 存档」**静默损坏**，而且 round-trip 测试还测不出来
> （导入按字段写入，缺的列保持原值，看起来"没丢")。

**导入语义**（写死，别有歧义）：

- **按 `id` upsert，只写文件里出现的列**，缺的列保持库中原值
- `set` 按 `name` 找；**找不到就新建**
- 可重复跑（幂等）

---

## 4. 后端模块

```
backend/app/eval/                 ← 新建
    __init__.py
    sets.py        评测集 + 用例的增删改查（分页 / 来源筛选 / 问题搜索）
    generate.py    从文档自动生成用例
    runner.py      跑一轮（挑题 → 走图 → 算题级指标 → 汇总）   ← 从 services/eval_service.py 搬来
    report.py      报告组装（综合得分 / 四张卡 / 逐题明细 / 结论句）
    ragas.py       隔离环境子进程调用                        ← 从 services/ragas_service.py 搬来
    config.py      消融开关语义                              ← 从 retrieval/eval_config.py 搬来
```

搬运要改的（已实测清点，共 8 个文件）：

| 文件 | 改动 |
|---|---|
| `app/api/eval.py` | `services.eval_service` → `eval.runner`；`retrieval.eval_config` → `eval.config` |
| `app/cli.py` | 同上（`cmd_eval_calibration`） |
| `app/graph/nodes/rewrite.py` | `retrieval.eval_config` → `eval.config` |
| `app/graph/nodes/retrieve.py` | 同上 |
| `app/graph/nodes/rerank.py` | 同上 |
| `tests/unit/test_ragas_service.py` | `services.ragas_service` → `eval.ragas`（连模块文档串一起改） |
| `tests/integration/test_eval_compare.py` | 只在文档字符串里提到 `eval_service._aggregate`，改注释 |
| **`app/schemas/eval.py`** | **不是搬 import，是必须同步扩字段——见下面的警告** |

> ⚠️ **`schemas/eval.py` 是最容易漏、后果最隐蔽的一处。**
> `GET /runs/{id}` 用 `response_model=EvalRunDetail`，而 FastAPI 会**静默丢弃**
> 响应模型里没声明的字段——不报错、不警告，前端就是收不到。
> 所以新加的 `report` 块、`set_name`、`done_cases`、`total_cases`、`error`
> **必须同步加进 schema**，否则整个报告弹窗是空的，而接口看着完全正常。
>
> 另：`EvalCaseResult.case_id: str` 是**必填**。§3.5 把它改成可空之后，
> 任何"用例被删过"的历史 run，`GET /runs/{id}` 会因响应校验失败 **500**。
> 必须改成 `case_id: str | None`。

> ⚠️ **`eval_config.py` 搬进 `app/eval/` 是负责人明确要求的**。代价是三个**检索侧图节点**
> 从此依赖评测模块（方向是反的：本来 `eval_config` 是检索开关语义，被图节点读）。
> 已确认接受。搬完必须跑全量测试——A9 条规定改这三处必须回归。

---

## 5. 接口

前缀不变：`/api/admin/eval`，全部 admin。

### 5.1 评测集

```
GET    /sets                       列表（每个集带用例数，见 §11.3-4）
POST   /sets                       新建 {name, description}
PATCH  /sets/{id}                  改名 / 改说明
DELETE /sets/{id}                  删除（连带它的用例；历史 run 不受影响）
GET    /sets/{id}/export           导出该评测集为 json 文件   ← 决策 21
```

**错误语义**：

| 情况 | 返回 |
|---|---|
| 建/改名撞已有名字（`name` 是 UNIQUE） | **409**，提示「已有同名评测集」 |
| 改/删/导出不存在的集 | 404 |
| **有 run 正在跑（pending/running）且它跑的就是这个集** | **409**，提示「该评测集正在被评测使用」。见 §5.5 的守卫 |
| 集里没有参与评测的用例 | 允许（删除/导出都正常），只是跑测时会失败并说明 |

`GET /sets/{id}/export` 的响应头：

```
Content-Type: application/json; charset=utf-8
Content-Disposition: attachment; filename*=UTF-8''<urlencoded 评测集名>.json
```

⚠️ **文件名要消毒**：评测集名里可能有 `/ \ : * ? " < > |` 这些在 Windows 上非法、
或会让 `Content-Disposition` 头断行的字符，服务端统一替换成 `_`。
用 `filename*=UTF-8''` 形式（RFC 5987）保证中文名不乱码。

> 空评测集也照常导出（`cases: []`），**不报错**——导出一套还没录题的题集是正常动作。

### 5.2 用例

```
GET    /sets/{id}/cases?source=&q=&page=&page_size=   列表 + 来源筛选 + 问题搜索 + 分页
POST   /sets/{id}/cases                               手工录入
POST   /sets/{id}/generate                            从文档自动生成 {document_id, count}
PATCH  /cases/{case_id}                               编辑（可改的列见下）
DELETE /cases/{case_id}                               删除（历史结果保留，case_id 置空）
GET    /cases/{case_id}/source                        「核对标准答案」的数据
```

**手工录入时服务端要自己填的字段**（弹窗里没有的，必须给值，否则 §3.3 那个
`case_type` 非空约束会当场报错）：

| 字段 | 值 | 为什么 |
|---|---|---|
| `id` | `uuid4().hex` | 和老题的人类可读 id（`f-01-01`）混用没问题，只要唯一 |
| `case_type` | `'factual'` | 手工录入的都是单轮事实题；`NOT NULL` 无默认值 |
| `suite` | 保持列默认 `'full'` | 老字段，不参与新逻辑，但别破坏它 |
| `set_id` | 路径里的那个集 | |
| `source` | `'manual'` | |
| `expected_doc_ids` | **留空** | 弹窗没这一项。**留空的后果见 §7.6** |

**可改的列**（`PATCH`）：`question` / `ground_truth` / `in_eval` / `note`。
**不可改**：`source`、`set_id`、`source_*`（改了就没有"来源"可言了）。

`GET /cases/{case_id}/source` 返回：

```json
{ "question": "...", "ground_truth": "...",
  "source_document_id": "...", "source_document_title": "...",
  "source_chunk_id": "...", "source_page": 3, "source_snippet": "...",
  "highlight": [12, 45] }
```

`highlight` 是标准答案在 `source_snippet` 里的字符区间 `[start, end]`（服务端算），前端据此高亮。
对不上时返回 `null`。

> ⚠️ **不能直接 `snippet.find(ground_truth)`。** 生成期的校验是 `make_eval_cases.py`
> 里的 `_squeeze()`——**去掉所有空白之后**再比子串。原因是规范化正文里有 PDF 提取
> 留下的硬换行（`…提出申请并经\n\n学院审核同意后送达；`），模型复述时自然写成一行。
> 所以标准答案在原文里**往往不是逐字连续子串**，`find()` 会经常返回 -1，
> 界面上就永远没有高亮。
>
> 正确做法：**按去空白口径定位，再把区间映射回原文偏移**（记录每个非空白字符
> 对应的原文下标，比对成功后取首尾映射回去）。

### 5.3 运行

```
POST   /run                       加 set_id；老的 suite / case_ids 继续认
GET    /runs?page=&page_size=     加：分页 + set_name / done_cases / total_cases / error / 耗时
GET    /runs/{id}                 加：报告块 + 逐题明细        ← 「看报告」弹窗吃这个
DELETE /runs/{id}                 新增。任务表那个「删除」按钮要用
GET    /compare                   原样不动
```

**`/runs` 要加分页**：现在只有 `limit`（`api/eval.py:73`），而参考图的任务表底下有分页。
改成 `page` / `page_size`（全站 `page_size ≤ 100`，§4.3.1.1）。
⚠️ `frontend/web/src/api/eval.ts` 的 `evalRuns(limit=20)` 与 `EvalPage.tsx`
的调用要跟着改。

**`DELETE /runs/{id}`**：删一轮 = 连它的逐题结果一起删（靠 §3.5 的 `ON DELETE CASCADE`）。
**删掉的 run 不再出现在 `/compare` 里**——这是有意的，删就是删。

### 5.4 ⚠️ 建 run 的两种模式（这条最容易搞错）

`POST /run` 的 `config` 决定这轮是**线上链路**还是**消融运行**，判据是
`retrieval/eval_config.py` 的 `switches()`——**config 字典为空 → None → 走线上默认**。

| 来源 | 请求体 | 落到 `eval_runs.config` | 实际跑什么 |
|---|---|---|---|
| **批量评测页**（新） | `{set_id, role, include_restricted}`，**不带 config、不带 suite** | `{}` | **线上完整链路** |
| 消融对比页（老） | 不带 config（或带开关） | `{"suite": "full"}` 或 `{"bm25": true, "suite": "full"}` | 纯向量基线 / 带开关的消融 |
| `run_ablation.py`、CLI | 同上 | 同上 | 同上 |

⚠️ **关键**：`api/eval.py` 现在无条件执行 `cfg["suite"] = payload.suite`。**必须改成：
只有走 `suite` 老路径时才塞 `suite`；走 `set_id` 新路径时 config 保持 `{}`。**
否则新页面的 run 又会被判成消融运行、又跑成纯向量基线——这正是 §1.1① 的坑。

`eval_config.label()` 在开头加一个分支：

```python
if not config:          # 真·空 config = 线上默认
    return "线上链路"
```

老 run 的 `{"suite": "full"}` 非空，仍返回「纯向量检索」——与它们**实际跑的行为一致**，不改。

> ⚠️ **同时必须修一个调用点**：`api/eval.py:69` 现在传的是 **`payload.config`**（请求体里的），
> 不是**落库的 `cfg`**：
>
> ```python
> return EvalRunCreated(..., config_label=eval_config.label(payload.config))   # ← 现在
> return EvalRunCreated(..., config_label=eval_config.label(cfg))              # ← 应该
> ```
>
> 不修的话：消融页点「跑全量」时 `payload.config` 是 `None` → 新分支返回**「线上链路」**，
> 而这一轮**实际跑的是纯向量基线**，列表里也显示「纯向量检索」——**创建响应的那一刻就在骗人**，
> 而且骗的方向和 §1.1① 正好相反。
>
> （`list_runs` / `get_run` 两处读的是库里的 `cfg`，本来就是对的，不用动。）

> `set_id` **只写 `eval_runs.set_id` 列，绝不进 `config`**（同 §3.4）。
> 进了 config 字典就非空 → `switches()` 返回非 None → 新页面又跑成纯向量基线。

### 5.5 选题的优先级（`runner.py`）

```
set_id 给了        → WHERE set_id = $1 AND in_eval = TRUE     ← 新页面（批量评测）
case_ids 给了      → WHERE id = ANY($1)                       ← 调试用，保持原样
case_type 给了     → WHERE case_type = $1                     ← ACL 对照实验用，保持原样
suite 给了         → 保持原 SQL，不碰集合                      ← 决策 22
      'full'          → SELECT * FROM eval_cases               （全部 90 条）
      'refusal_calib' → WHERE suite='refusal_calib'            （15 条）
都不给             → 全部                                     ← 兜底，保持原样
```

#### `suite` 路径为什么不碰集合（决策 22）

现在 `suite='full'` 实际跑的是**全部 90 条**（§1.1⑤）。按 `suite` 分组迁进两个集，
它就只剩 75 条——差的那 15 条是拒答校准题，大多是**该拒答**的，混进来会拉低召回率。
一旦跑全量从 90 变 75：

- 消融 8 行表的**历史行与新行题集不同**，表里的差值不再只反映配置差异
- §11.2 那句「老 run 与新 run 不可比，但 `config_label` 能区分」就**失效了**——差异不止来自链路

所以 `suite` 老路径**一行 SQL 都不动**，继续直接查 `eval_cases`。新页面走 `set_id`。

> **两条件量不同是正常的**，不是 bug：一个是"全部题库"，一个是"这一套题"。

#### `in_eval` 只对 `set_id` 路径生效

`suite` 路径（CI 的 `eval-calibration`、`run_ablation.py`）**不过滤** `in_eval`。
理由：那些入口的题集必须稳定——有人在界面上随手关掉一条校准题，就把 CI 门禁的分母改了，
甚至可能直接空集导致 CI 变红。这种事不该由一次误点触发。

**代价要如实写明**：同一道题，在批量评测页关掉后不再参与，但在消融/CI 里照样跑。
所以界面上那个开关的含义更接近「参与批量评测」。

#### 选到空集时

直接标 `failed`，**不跑出一个空报告**：

- `set_id` 路径 → `error = "这个评测集没有参与评测的用例"`
- `suite` 路径 → 保持现有的 `no_cases` 行为

#### 跑测期间的删除守卫（本轮新增）

`runner.py` 在开头把题**一次性读进内存**再逐题跑。若跑到一半有人删了这个评测集
或其中一条用例，最后落库时 `case_id` 指向已不存在的行 → 外键报错 → **整轮 `failed`**。

守卫：**有 run 处于 `pending`/`running` 时，禁止删它正在跑的那个评测集、以及该集的用例**，
返回 409 并说明。系统本来就「一次只允许一轮」（`api/eval.py:50`），所以检查很轻。
删**别的**评测集不受影响。

---

## 6. 前端

### 6.1 侧栏

```
仪表盘
文档管理
版本管理
拒答分析
用户管理
▣ 效果评测                  ▾
    评测集管理   →  /admin/eval/sets
    批量评测     →  /admin/eval/runs
    消融对比     →  /admin/eval/ablation
```

`/admin/eval` 重定向到 `/admin/eval/sets`。分组可折叠、默认展开。

### 6.2 页面一：评测集管理

```
[售后问题测评（5 条用例）▾]  [新建评测集] [删除评测集] [导出评测集]
──────────────────────────────────────────────────────────
[手工录入] [从文档自动生成]   [按来源筛选▾] [按问题搜索____] [查询]
──────────────────────────────────────────────────────────
问题                  │标准答案    │来源    │参与评测│备注│操作
──────────────────────┼───────────┼────────┼───────┼───┼─────────────────
有偿维修的部件自维修...│3个月      │文档生成 │  是   │   │[看原文][编辑][删除]
…                                                              [< 1 >]
```

- **「从点踩沉淀」按钮不做**（决策 3）
- 筛选与搜索**走后端**，不前端过滤
- 四个弹窗：新建评测集 / 评测用例（手工录入与编辑共用）/ 从文档自动生成用例 / 核对标准答案

**「导出评测集」按钮**（决策 21，在最右边）：

- 导出**当前下拉里选中的**那个评测集，存成 `售后问题测评.json`
- 走 `requestBlob` 取回文件再触发浏览器下载——**不能直接 `<a href="/api/...">`**，
  因为那条路带不上 `Authorization` 头（`client.ts` 里 `requestBlob` 的注释已经写明这个坑）
- 未选中任何评测集时按钮**置灰**
- 导出成功后给一句提示（如「已导出 5 条用例」），失败走和其他按钮一致的错误提示

### 6.3 页面二：批量评测

```
评测集 [售后问题测评（5 条用例）▾]   角色 [学生▾]    [开始评测]
评测会逐条调用模型和重排，一轮下来是分钟级。建议先用 5~10 条验证方向。
──────────────────────────────────────────────────────────
任务ID│评测集       │状态   │进度│失败原因│耗时(ms)│时间               │操作
──────┼────────────┼──────┼───┼───────┼───────┼──────────────────┼──────────
 508  │售后问题测评 │评测中 │0/5 │       │       │2026-09-07 12:10:49│[看报告][删除]
──────────────────────────────────────────────────────────
                                                    [< 1 >]
```

- **「检索策略」列不做**（决策 4）
- 进度每 1~2 秒轮询
- 提示文案**不编数字**——参考图里的"六次模型""几十万 token"本仓库没有依据，改成上面那句
- 状态中文映射：pending=排队中 / running=评测中 / done=已完成 / failed=失败
- **`done_cases == total_cases` 但 `status` 仍是 running** 时，进度栏显示「正在计算指标…」
  ——因为 ragas 是整批算的，跑满 5/5 之后还有一段
- **顶部只有 `[评测集▾] [角色▾] [开始评测]`**，没有提权勾选框（`include_restricted`
  恒为 false）。要跑提权对照实验走消融对比页

#### 「看报告」要等评测跑完才可点（决策 24）

- `status` 是 `pending` / `running` 时，按钮**置灰**，悬停提示
  「评测还在跑，完成后才能看报告」
- 工具提示里带上进度：「评测还在跑（3/5），完成后才能看报告」
- 理由：半截的平均分最容易被截图当结论发出去。**宁可不出数，不出会误导的数**
  （比参考图更严：图里"评测中"那行的按钮是可点的）

#### 跑完弹飘窗（决策 24）

轮询发现某一轮从 `running` 变成 `done`（或 `failed`）时，弹一个飘窗：

```
┌────────────────────────────────────┐
│ 评测完成                            │
│ 售后问题测评（5/5）可以看报告了       │
└────────────────────────────────────┘        ← 几秒后自动淡出
```

- **自动消失**，不挡操作、不用点确定
- `failed` 也弹，文案不同：「评测失败」+ 失败原因
- **用 `@base-ui/react` 自带的 Toast**（v1.8.0 已装，`toast/` 下有
  Provider / Root / Content / Title / Description / Close / Viewport / `useToastManager`）
  ——⚠️ **不需要装 sonner 或别的新依赖**，项目里本来就有
- 需要在轮询里**记住上一轮的状态**才能识别"刚刚跑完"这个跃迁：
  现在的 `ensurePolling` 拿到新列表就覆盖了旧的，要在覆盖前比对
- 飘窗只在这一处触发。其他地方（导出成功、生成完成）沿用现有的**行内文字提示**
  （`EvalPage.tsx` 已有的 `setMsg` 模式），不铺开

### 6.4 页面三：消融对比

**分两部分，别混为一谈：**

**① 原样搬（现有能力）**：角色 / 提权 / 跑全量 / 跑校准小集 两个按钮 /
历史轮次表（勾选）/ 消融对比矩阵。只改导航位置与标题。

**② 新增：六个消融开关（决策 23）—— 本仓库原本没有这个界面。**

前端**从来就产不出非基线的 run**（§1.1④）：`EvalPage.tsx` 里没有开关、
`startEvalRun` 也不传 `config`，所有消融行都是 `run_ablation.py` 脚本打出来的。
光把页面搬过去，它仍然只能产出纯向量基线。

要补的：

```
☐ BM25   ☐ RRF   ☐ 精排   ☐ verbatim 查询   ☐ keywords 查询   ☐ hyde 查询
```

- 六个复选框，勾选后组装成 `config` 发出去（键名必须是
  `bm25` / `rrf` / `rerank` / `expand_verbatim` / `expand_keywords` / `expand_hyde`，
  与 `eval_config.SWITCH_KEYS` 一致）
- 全不勾 = 纯向量检索（就是现在的"跑全量"行为，**保持不变**）
- 勾选状态要能看出这轮跑的是什么配置——服务端返回的 `config_label` 已经会拼
  （「纯向量检索」/「开启 BM25、RRF」/「完整链路」）
- ⚠️ 这个页面的 run **仍然走 `suite` 路径**（不是 `set_id`），所以 config 里照旧带 `suite` 键
  ——这正是它被判成消融运行的原因，也是它该有的行为

这是**决策 4 的例外**：检索策略的**开关**在这里，不在批量评测页。§4.3.1.4 的契约与
`run_ablation.py`、`test_eval_compare.py` 全部照旧。

### 6.5 弹窗：核对标准答案

```
┌─ 核对标准答案 ─────────────────────────── × ─┐
│  问题                                        │   ← 小标题加粗
│  有偿维修的部件自维修完成之日起保修多长时间？    │
│                                              │
│  标准答案                                     │   ← 小标题加粗
│  3个月                                        │
│                                              │
│  来源片段原文                                  │   ← 小标题加粗
│  （模型就是照着这段出的题，对不上说明这条标准答案不能用）│
│ ┌──────────────────────────────────────────┐ │
│ │ 片段 #237 · 第 5 段 · 第 3 页              │ │
│ │                                          │ │
│ │ 。备机为同型号或更高型号，配置由我方工程师    │ │
│ │ 协助导入。逾期未归还的，按设备原价的 1%      │ │
│ │ 每日收取占用费。                          │ │
│ │ 第七条 有偿维修                            │ │
│ │ 一、不属于免费保修范围的故障，按实际更换的    │ │
│ │ 物料成本加人工费计收…                      │ │
│ │ 二、有偿维修的部件自维修完成之日起保修 3 个月。│ │← 高亮
│ │                                          │ │
│ │ 三、检测后确认无故障的，收取检测费 200 元…   │ │
│ │ 第八条 技术支持内容                        │ │
│ │ 一、产品选型建议、点位规划建议              │ │
│ └──────────────────────────────────────────┘ │
│                                      [ 关闭 ] │
└──────────────────────────────────────────────┘
```

- **「问题」「标准答案」「来源片段原文」三个小标题加粗**（负责人明确要求）
- 比参考图多一处：**标准答案在原文里高亮**。生成时已硬校验过"标准答案必须是原文的连续子串"，
  定位是免费的；高亮之后"对得上/对不上"一眼可见，不用人肉眼比对
- `第 X 段` 不存库，按片段内段落序号现算（片段内第几段）
- **没有来源片段的用例，按钮置灰**。判据是 **`source_snippet` 是否为空，不是 `source` 字段**
  ——老 90 条迁移后 `source='generated'` 但**没有** `source_*`（§9 不回填它们）。
  按 `source` 判会出现「列表里标着『文档生成』、点开却被告知『这是手工录入』」的自相矛盾
- 悬停提示分两种：
  - `source='manual'` → 「手工录入的用例没有来源片段」
  - `source='generated'` 但 snippet 为空 → 「这条是早期导入的题库，没有留下来源片段」

### 6.6 弹窗：评测报告

由批量评测页的「看报告」打开。**界面完全按参考图**：

```
┌─ 评测报告 ──────────────────────────────────────────── × ─┐
│ 评测集：售后问题测评  策略：线上链路  用例 5 条  综合得分 0.7988│
├──────────────────────────────────────────────────────────┤
│ ┌上下文召回[找资料]┐┌上下文精度[找资料]┐┌忠实度[写答案]┐┌答案相关性[写答案]┐│
│ │Context Recall   ││Context Precision││Faithfulness ││Answer Relevancy││
│ │     0.8         ││      0.74       ││    0.8      ││    0.855      ││
│ │标准答案里说的东西，││找回来的资料里，  ││答案里每句话，││答案是在回答这个 ││
│ │检索找回来了吗？  ││有用的排在前面吗？││在资料里有出处？││问题吗？        ││
│ │达标（≥0.7）     ││达标（≥0.6）     ││达标（≥0.8） ││达标（≥0.7）   ││
│ └─────────────────┘└─────────────────┘└─────────────┘└───────────────┘│
├──────────────────────────────────────────────────────────┤
│ 四项指标都在合理区间。                                     │
├──────────────────────────────────────────────────────────┤
│问题│标准答案│生成的答案│召回│精度│忠实度│相关性│均分│失败原因  │
│ …                                                        │
│                                                [ 关闭 ]   │
└──────────────────────────────────────────────────────────┘
```

**版式细节**（照参考图逐字核过）：

| 卡片 | 徽标 | 提示语（逐字） | 达标线 |
|---|---|---|---|
| **上下文召回** Context Recall | 找资料 | 标准答案里说的东西，检索找回来了吗 | 达标（≥ 0.7） |
| **上下文精度** Context Precision | 找资料 | 找回来的资料里，有用的排在前面吗 | 达标（≥ 0.6） |
| **忠实度** Faithfulness | 写答案 | 答案里每句话，在资料里有出处吗（有没有编） | 达标（≥ 0.8） |
| **答案相关性** Answer Relevancy | 写答案 | 答案是在回答这个问题吗（有没有跑题） | 达标（≥ 0.7） |

- 中文名**粗体** + 英文名**灰色小字**压在下面
- 分数**大号**：达标绿色、**未达标红色**（参考图四项全达标所以全是绿的，没展示反面）
- 达标行绿色小字；结论条浅绿底；卡片浅灰底、圆角

---

## 7. 报告的口径

### 7.1 服务端算，不由前端拼

与 §4.3.1.4 里 `config_label` 由服务端派生**同一个理由**：指标会演进，前端拼公式就会出现
「同一份数据算出两个分」。

`GET /runs/{id}` 返回：

```json
{
  "report": {
    "composite_score": 0.7988,
    "metrics": [
      {"key": "context_recall",    "score": 0.8,   "threshold": 0.7, "passed": true},
      {"key": "context_precision", "score": 0.74,  "threshold": 0.6, "passed": true},
      {"key": "faithfulness",      "score": 0.8,   "threshold": 0.8, "passed": true},
      {"key": "answer_relevancy",  "score": 0.855, "threshold": 0.7, "passed": true}
    ],
    "verdict": "四项指标都在合理区间。",
    "ragas_available": true,
    "ragas_errors": []
  },
  "cases": [
    { "case_id": "...", "question": "...", "ground_truth": "...", "answer": "...",
      "context_recall": 1, "context_precision": 0.7, "faithfulness": 1,
      "answer_relevancy": 0.9951, "score": 0.9238, "error": null }
  ]
}
```

> ⚠️ **`ragas_available` / `ragas_errors` 必须在 `report` 里**，不能只在 `metrics` 里。
> §11.2 要求「`.venv-ragas` 不在时前端显式提示并指向文档，不做哑谜」——没有这两个字段，
> 报告页四张卡全「—」时前端**无从解释**，那就正是哑谜。
> 取值直接取自 run 的 `metrics.ragas_available` / `metrics.ragas_errors`。
>
> 三种"空"要能分开（沿用 `EvalPage.tsx` 现在的诊断口径）：
> ① 环境不在 → `ragas_available: false` + 原因；
> ② 跑了但有指标没算出来 → `ragas_errors` 非空；
> ③ 环境好、没报错、四项却都空 → 这一轮没有可评分的样本（缺标准答案/上下文）。

**中文名（"上下文召回"）与提示语放前端** —— 与现有 `METRIC_LABEL` 的做法一致。
**阈值与达标判断放服务端** —— 那是判断，不是文案。

> ⚠️ **键名要拍平**：库里逐题的键是带前缀的 `metrics.ragas_context_recall`，
> 整轮的键是嵌套的 `metrics.ragas.context_recall`（两处不一致，是历史遗留）。
> 报告接口**统一吐成不带前缀、不嵌套的 `context_recall`**，前端只认这一种形状。
> 拼接在服务端做，别让前端去猜该读哪个键。

### 7.2 算式（从参考图反推，已逐项对上）

| 量 | 算式 | 验算 |
|---|---|---|
| 四张卡 | 该项在**所有算出了分的题**上的算术平均 | 召回列 (1+1+0+1+1)/5 = 0.8 ✓ |
| **综合得分** | 四张卡的算术平均 | (0.8+0.74+0.8+0.855)/4 = 0.79875 → 0.7988 ✓ |
| 逐题**均分** | 该题四项的算术平均 | 行1 (1+0.7+1+0.9951)/4 = 0.9238 ✓ |
| 逐题召回/精度 | ragas 的 `context_recall` / `context_precision` | ✓ |
| 耗时(ms) | `finished_at - started_at` | |

### 7.3 缺项怎么显示（决策 13）

- 某张卡**一项都没算出来** → 该卡显示「—」，不显示 0
- **综合得分：四项齐全才出**；缺任何一项显示「—」并说明缺哪项
- **逐题**：只要该题有**任一项**缺失 → **该项与该题的「均分」都显示「—」**，
  不用剩下的三项去凑一个均分

理由都一样：拿不齐的数凑平均值，比不给数更误导——它看起来像个结论。
与现有 `ragas_available: false` 时不假装有数是同一口径。

> 别和参考图第 3 行混了：那一行四项是 `0 / 0 / 0 / 0.385`、均分 `0.0962`——
> 那是**四项都拿到了分、且真的都是 0**（它确实有标准答案「换货」，只是检索没找到）。
> **缺项不该出现数字。**

### 7.4 「失败原因」列的口径（决策 20）

参考图里 5 行**全是空的**——包括第 3 行「知识库中未找到相关内容」（召回 0、均分 0.0962）。
说明口径是：**只有这道题真跑挂了（抛异常）才填**。

**拒答不算失败**——"系统正确地说了不知道"是个正常结果，而且「生成的答案」那列已经把原因
写着了。所以这一列大多数时候是空的，**这是对的，不是没做**。

### 7.5 结论句

- 四项全部达标 → **「四项指标都在合理区间。」**
- 有未达标 → 列出哪几项没达标、差多少
- 无从判断（缺项） → 说明缺哪项

### 7.6 ⚠️ 手工录入的用例会拉低 Recall@5 / MRR —— 必须改

这条**不影响新报告**（报告只吃 ragas 四项），但**影响消融对比表**，所以本轮必须一起改。

`runner.py` 的题级指标用 `expected_doc_ids` 算 `recall_at_k` / `mrr`：

```python
expected_ids = set(expected or [])        # 空 → 空集
first_hit = next((i for i, d in enumerate(doc_ids, start=1) if d in expected_ids), 0)
```

手工录入的用例**没有** `expected_doc_ids`（弹窗里没这一项，见 §5.2）→ `first_hit` 恒为 0
→ `recall_at_k` / `mrr` 恒为 **0 分**。而 `_aggregate` 的 `_avg()` 照样把它算进均值
→ **整轮的 Recall@5 / MRR 被这批题凭空拉低**，而且看不出原因（逐题单看都"正常"）。

**改法**：没有 `expected_doc_ids` 的题，`rank` / `recall_at_k` / `mrr` 返回 **`None`（不是 0）**。
`_avg()` 现有的 `isinstance(v, (int, float))` 过滤会自动跳过 `None`，**聚合逻辑一行都不用改**。

同时 `_aggregate` 里 `with_gt` 那段（数"有参考答案的题"）也要跟着排除，
否则 `scored_with_ground_truth` 会虚高。

> 老 90 条全都有 `expected_doc_ids`，所以这条**只影响新录的题**——不影响消融历史。

---

## 8. 从文档自动生成

### 8.1 复用旧脚本的口径，不另发明

沿用 `tools/make_eval_cases.py` 那套**已经在用**的做法：

```
1. 从向量库取出这份文档的 chunk（带页码、片段号）——不是自己重切一遍
2. 均匀挑 N 个片段（N = 生成条数，上限 10，默认 5）
3. 每个片段问模型一次：出一道题 + 答案必须逐字抄自这一段
4. 硬校验，不过就重试（最多 2 次）：
     ① 标准答案必须是这段原文的**连续子串**（忽略空白）
     ② 问句里**不许有代词**（这个/那个/该/其…）
5. 通过的入库 —— 字段清单见下（一个都不能漏）
```

**生成用例要写的字段**（旧脚本 `make_eval_cases.py` 写过其中一部分，别漏）：

| 字段 | 值 | 为什么 |
|---|---|---|
| `id` | `uuid4().hex` | 同 §5.2 |
| `question` / `ground_truth` | 模型产出（已过硬校验） | |
| `case_type` | `'factual'` | **`NOT NULL` 无默认值**，不填就报错 |
| `set_id` | 路径里的那个集 | |
| `source` | `'generated'` | |
| `expected_doc_ids` | `[source_document_id]` | **必须写**：不写这道题在轮次汇总里的 `recall_at_k`/`mrr` 恒为 0（见 §7.6）。出题就是从这份文档出的，期望文档就是它 |
| `expected_route` | `'knowledge'` | 旧脚本写它；A 组路由指标要用 |
| `should_clarify` | `0` | 同旧脚本口径（单轮题不该触发澄清） |
| `source_document_id` / `source_chunk_id` / `source_page` / `source_snippet` | 从 Chroma 取到的片段信息 | 「核对标准答案」要用 |

`turns` / `visible_roles` / `expected_chunk_ids` **保持 NULL**——生成的是单轮、非受限题。

**为什么必须从向量库取 chunk 而不是自己重切**：旧脚本读规范化正文、自己 `chunk_text()`
切一遍。但检索看到的是**入库时切的那份**——两套切法一旦不一致，"来源片段"就是假的。
从 Chroma 取还能白拿 `page` 与 `chunk_index`。

**为什么校验②是必须的**：旧脚本的实测教训——单轮题带代词（"这个细则管的是哪些学生？"）
会被图的 `resolve` 节点正确判成「指代不明」→ 走 `clarify`，于是评测里凭空多出
「路由错」的假失败。代词是多轮题库在干的活儿。

### 8.2 同步、有上限、超时给部分结果（决策 18）

- **同步等待**，不引入后台任务。前端弹窗显示「生成中…」
- 并发 3 路；5 条约 20~30 秒
- **总超时 120 秒**。超时返回**部分结果**（已生成的入库，未生成的说明原因）
- **条数是目标不是保证**：要 5 条、只过了 4 条就是 4 条。
  前端如实提示「要了 5 条，实际生成 4 条（1 条没通过校验）」——**不硬凑**
- 生成完**直接入库**，不搞"先预览再确认"。用「核对标准答案」逐个核验、不行的删掉

**`POST /sets/{id}/generate` 的响应形状**（前端要拿它显提示，不定义就没法做）：

```json
{ "requested": 5, "created": 4, "failed": 1,
  "reason": "1 条没通过校验（标准答案不是原文逐字子串，或问句里带了代词）",
  "timeout": false,
  "cases": [ { "id": "...", "question": "...", "ground_truth": "..." } ] }
```

- `created < requested` 时前端显示「要了 5 条，实际生成 4 条（1 条没通过校验）」
- `timeout: true`（总超时 120 秒）时前端显示「超时，已先生成 4 条，剩下的可以再点一次」
  ——**已经生成的要保留**，不能因为超时把库里的删掉
- `cases` 只回必要字段（前端生成完要刷新列表，不需要整行）

### 8.3 为什么 `source_snippet` 必须存（决策 12）

`chunk_index` 与页码在**文档重新索引后会变**（重新切块，边界会漂）。只有把那段原文抄下来，
三个月后打开还看得见"当初模型是照着什么出的题"。

这正好解释了参考图里那句「对不上说明这条标准答案不能用」——**对不上，往往是文档更新过了**。

---

## 9. 数据迁移

一个迁移文件 `004_eval_sets.sql`。

> ⚠️ **每一步都必须幂等。** `cli.py` 的 `init-db` **每次都按序重跑 `migrations/*.sql`**
> （表头自己写着「幂等」，002/003 都是按这个标准写的）。不幂等有两个后果：
> ① 重跑报错，中断整条迁移链；② 上线后有人把某题的 `source` 改成 manual，
> 运维重跑一次 `init-db` 就被改回 generated ——**静默改用户数据**。

```sql
-- ① CREATE TABLE IF NOT EXISTS eval_sets (...)
-- ② ALTER TABLE ... ADD COLUMN IF NOT EXISTS
--      eval_cases 8 列 / eval_runs 5 列 / eval_case_results 3 列（见 §3.3-§3.5）
-- ③ 外键与约束（都要 IF EXISTS / 存在性判断，不能重复加）：
--      eval_case_results.case_id  DROP NOT NULL
--      eval_case_results.case_id  外键 → ON DELETE SET NULL
--      eval_case_results.run_id   外键 → ON DELETE CASCADE
--    ⚠️ ADD CONSTRAINT 不能重复加：用
--       DO $$ ... IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='...') $$ 包
-- ④ 建两个默认评测集：INSERT ... ON CONFLICT (name) DO NOTHING
-- ⑤ 回填归属（**只补 NULL 的**，别覆盖用户后来改过的）：
--      UPDATE eval_cases SET set_id = <默认题库>   WHERE suite='full'          AND set_id IS NULL;
--      UPDATE eval_cases SET set_id = <拒答小集>   WHERE suite='refusal_calib' AND set_id IS NULL;
-- ⑥ 回填来源（决策 19）：
--      ⚠️ 不能写无条件 UPDATE —— 重跑会把用户自己改成 manual 的题改回去。
--      加条件限定"迁移前就存在的那批 id"（用 fixture 里的 id 列表最稳妥）。
-- ⑦ 【必做，否则老报告全是空的】
--      UPDATE eval_case_results r
--         SET question = c.question, ground_truth = c.ground_truth
--        FROM eval_cases c
--       WHERE r.case_id = c.id AND r.question IS NULL;
--      §3.5 说加了快照列就"历史报告自给自足"，但库里已有的 112 行
--      question/ground_truth 全是 NULL。不回填的话，打开任何一轮历史报告，
--      「问题」与「标准答案」两列都是空的。
```

**⑥ 的说明**：90 条里绝大部分是模型出的，少数多轮题来自题库文件，都**不是在界面上
手工敲的**，所以标 generated 比标 manual 更准。但正因为它是"改数据"，才更要幂等。

**「默认题库」是 75 条，不是 90 条。** `suite='full'` 的 75 条进「默认题库」，
`suite='refusal_calib'` 的 15 条进「拒答校准小集」。而**老的 `suite` 路径照旧跑全部 90 条**
（决策 22）—— 两件事互不干扰，但读文档的人容易混，所以在这里明写一遍。

**顺带要改 `cli.py` 的表清单**：`cmd_init_db` 里硬编码了一份 12 张表的 `expected` 集合
（`cli.py:54-58`），缺表会报错。不把 `eval_sets` 加进去、不同步那句「✅ 12 张表齐全」，
新表建没建成它都不检查。

迁移后手动跑一次 `eval_set_io.py export`，把两个 json 生成出来提交进仓库。

**消融对比页与既有脚本一行都不用改**：`suite` 路径保持原 SQL、完全不碰集合（决策 22），
`run_ablation.py`、`cli.py`、老测试全部照常。

---

## 10. 测试

### 10.1 新增

| 测什么 | 关键断言 |
|---|---|
| 评测集增删改 | 删集**连带删用例**，但历史 run 还在（`set_name` 快照可读） |
| 评测集重名 | 返回 409，不是 500 |
| 用例增删改查 | 来源筛选、问题搜索、分页边界；**手工录入能插进去**（`case_type` 有值） |
| **删用例不动历史** | 删完再查那轮的旧报告，那行**还在且内容完整**（`case_id` 为 `null` 也不 500） |
| **删 run** | 有逐题结果时也能删掉（`run_id` 已改 CASCADE） |
| 报告算式 | 综合得分 = 四卡均值；均分 = 该题四项均值 |
| 缺项 | 缺一项 → 综合得分是 `null`，**不是**三项的平均；**该题均分也是 `null`** |
| 达标边界 | 四条线的边界值：0.7 / 0.6 / 0.8 / 0.7 |
| 进度计数 | 跑一轮，`done_cases` 从 0 递增到 `total_cases` |
| 参与评测开关 | `set_id` 路径：`in_eval=false` **不进**这一轮；`suite` 路径：**照样进**（§5.5） |
| 建 run 两种模式 | `set_id` 路径的 config 落库是 `{}`；`suite` 路径带 `suite` 键 |
| **`config_label` 两处一致** | 创建响应里的 label 与列表里的 label **必须相同**（§5.4 那个调用点） |
| **`suite` 路径题量不变** | `suite='full'` 仍选中**全部 90 条**，不是 75（决策 22） |
| **手工用例不拉低召回率** | 一条无 `expected_doc_ids` 的题 + 一条有且命中的题 → 轮次 `recall_at_k` 是 **1.0 不是 0.5**（§7.6） |
| 自动生成 | mock 模型**故意返回改写过 / 带代词的答案，必须被拦下**；返回原文字串则通过；生成的题要带 `expected_doc_ids`（§8.1） |
| 自动生成超时 | 超时后**已生成的留在库里**，响应 `timeout: true` 且 `created` 是实际条数 |
| 单题失败要存原因 | 修完 §1.1③ 后，`eval_case_results.error` 有值 |
| 导出评测集 | 导出→导入 round-trip **逐字段比对**（含 `turns`/`visible_roles`/`expected_route`/`should_clarify`，别只比问题答案）；**文件名消毒**（名字含 `/` 时不产生断行/多级路径）；空集导出 `cases: []` 而不是报错 |
| **来源片段高亮** | 标准答案里带 PDF 硬换行（原文有 `\n`、答案没有）时**仍能定位**（§5.2 的去空白口径） |
| **删除守卫** | 有 run 在跑它时，删该评测集/该集的用例 → 409；删**别的**集不受影响 |
| **迁移幂等** | **连续跑两次 `init-db`**，`eval_cases`/`eval_sets`/`eval_runs` 的行数与内容都不变 |
| **迁移回填** | 迁移后老结果行的 `question`/`ground_truth` **非空**，老轮次报告的两列有内容 |

> 决策 24 是纯前端行为（按钮置灰 + 飘窗），不写后端测试，靠手测 / bsk 走一遍。

### 10.2 回归

现有 4 个评测测试 + 全部老测试必须全绿：

```
backend/tests/unit/test_ragas_service.py
backend/tests/integration/test_eval_compare.py
backend/tests/integration/test_eval_cases.py
backend/tests/integration/test_eval_fixture.py（unit 里也有一个同名的）
```

> ⚠️ A9 规定：改 `eval_config.py` 的 import 位置会同时碰到三个图节点，
> **必须跑全量测试**，不能只跑评测那几个。

---

## 11. 已知风险与不做的事

### 11.1 明确不做

| 不做 | 理由 |
|---|---|
| 「从点踩沉淀」按钮 | 负责人指定 |
| 「检索策略」下拉/列 | 负责人指定（开关仍在消融对比页） |
| 「知识库」概念 | 项目没有这一层，负责人指定跳过 |
| 增删改用例时**自动**写盘 | 决策 8：导出要有明确动作（按钮或命令行），不做隐式落盘 |
| 界面上的**导入**按钮 | 本轮只做导出；导入仍走 `eval_set_io.py import` |
| 改点踩/拒答标注相关功能 | 与本次无关 |

### 11.2 风险

| 风险 | 影响 | 处置 |
|---|---|---|
| **搬 `eval_config.py` 会碰三个图节点** | 检索链路可能被改坏 | 搬完跑全量测试；这是本方案唯一动到主链路的改动 |
| **`.venv-ragas` 不在时报告全空** | 批量评测页四张卡全是「—」 | 服务端返回 `ragas_available:false` + 原因，前端显式提示并指向 `docs/评测与ragas.md`。**不做哑谜** |
| **ragas 有跑间随机性** | `answer_relevancy` 同一样本两次跑出 0.8901 与 0.712 —— **差值 0.178**。这个数比 `docs/评测与ragas.md` 里写的「差值 < 0.15 不作判断依据」**门槛还大**，那条口径本身就自相矛盾 | 报告里**如实标注实测抖动可达 ~0.18**，别拿一个比实测噪声还小的门槛去下结论。要更稳就给判官固定 seed 或调 n（见该文档"已知问题 #1"） |
| **老 run 与新 run 分数不可直接比** | 消融表里混着纯向量基线与线上链路的轮次 | `config_label` 会分别显示「纯向量检索」与「线上链路」。**题量不一致的问题已由决策 22 消除**（`suite` 路径仍跑全部 90 条），两边差异只剩链路本身 |
| **文档重新索引导致来源对不上** | 「核对标准答案」显示"对不上" | 这正是该功能要暴露的问题；`source_snippet` 快照保证看得见当初的原文 |

### 11.3 施工时的小决定（已定，写在这里免得上手时再想）

1. `Button` 现有 6 个变体：`default` / `outline` / `secondary` / `ghost` / `destructive` / `link`
   ——**没有"成功绿"**，且 `destructive` 是**浅红底**（`bg-destructive/10`），
   不是参考图那种实心红。参考图用了实心绿/蓝/红三色。
   **加 `success` 变体 + 一个实心红的变体**，别在每处就地写 className。
2. `eval_runs.status` 有 CHECK 约束限制四个值。**不动它**——"算指标中"由
   `done_cases == total_cases && status == 'running'` 推导，不需要新状态。
3. 分页上限沿用全站 `page_size ≤ 100`（§4.3.1.1）。
4. 评测集下拉里的「（N 条用例）」= 该集**全部**用例数（不是只数 `in_eval=true` 的）
   ——它与界面上的行数对得上，用户才不会以为丢了数据。
5. 前端目前**没有"点一下存到本地"的先例**（`requestBlob` 现在只把 blob 喂给 PDF 阅读器）。
   导出按钮要新写一个小工具函数（`URL.createObjectURL` + 造一个 `<a download>` + 用完 `revokeObjectURL`），
   放 `lib/` 下——报告导出（若日后要做）也会用它。
6. **弹窗里不做内联的 `【第 N 页】` 分页标记**（参考图里有）。原因：Chroma 的 chunk
   metadata 只记了**起始页**（`page`），而 PDF 的 chunk **可以跨页**——要标出页与页的
   分界，得额外存一份"片段内的页码断点"。定位行上的「第 Y 页」已经有了同样的信息，
   为一个装饰性标记加一份数据不划算。要做的话是后续增量。
7. **`eval_runs` 不加 `current_case` 列**。原本想用它显示"正在跑第几题"，
   但参考图的任务表只有 `进度 0/5`，没有这道题的出口——加了没人消费。
   要排查"卡住了吗"，看 `done_cases` 有没有在动就够了。
8. 任务表里「角色」下拉是**批量评测页独有**的，消融对比页另有自己的一套
   （角色 / 提权 / 六个开关）。两页各自发起 run，共用同一个 `/runs` 列表。
9. **飘窗用 `@base-ui/react` 的 Toast，不加新依赖**（API 已实测确认）。
   项目已经装了 `@base-ui/react@1.8.0`，`toast/` 下有完整实现。
   ⚠️ **别去装 `sonner`** —— shadcn 文档默认推荐它，但那会白加一个依赖。

   ```
   <Toast.Provider> + <Toast.Viewport>  挂到 AdminLayout（或 App 根）
   const toast = useToastManager()       在页面里用
   toast.add({ title: '评测完成', description: '…' })
   ```

   `add()` 返回 toast id；**默认 5000ms 自动消失**（`timeout: 0` 才不消失）；
   自动带 `priority: 'low' | 'high'` 的无障碍播报级别；悬停/聚焦会暂停计时。

---

## 附录：决策溯源

本文每条决策都来自 2026-10-07 的逐条确认。参考图共 8 张，与本方案的对应关系：

| 参考图 | 落到哪 |
|---|---|
| 前端导航目录 | §6.1（多一个"消融对比"，负责人要求保留） |
| 评测集管理模块 | §6.2 |
| 新建评测集按钮 | §6.2 弹窗一 |
| 手工录入按钮 | §6.2 弹窗二 |
| 从文档自动生成按钮 | §6.2 弹窗三、§8 |
| 批量测评模块 | §6.3（去掉检索策略**列**、加角色下拉）。参考图顶部那个「检索策略」**下拉**按决策 4 不做，但它背后的六个开关按决策 23 移到了消融对比页 |
| 评测报告按钮 | §6.6、§7 |
| 查看原文（核对标准答案） | §6.5（多一处"标准答案高亮"） |

参考图之外的追加（负责人另行要求，图上没有）：

| 追加 | 落到哪 | 理由 |
|---|---|---|
| 「导出评测集」按钮（决策 21） | §5.1 / §6.2 / §3.6 | 参考图没有导出入口；加它是为了让评测集能带出系统、能 git 存档 |
| 侧栏第三项「消融对比」（决策 9） | §6.1 / §6.4 | 参考图只有两项；保留它是为了不丢 §4.3.1.4 的既有交付物 |
| 六个消融开关 UI（决策 23） | §6.4 | 参考图把"检索策略"放在批量评测页顶部（决策 4 不做）；但本仓库前端**从来没有**这个界面，消融页光搬过去产不出非基线 run，所以补在消融页 |
| `suite` 老路径不碰集合（决策 22） | §5.5 / §9 | 参考图没有历史包袱；这是本仓库特有的兼容要求 |
| 「看报告」跑完才可点 + 跑完弹飘窗（决策 24） | §6.3 | 参考图里"评测中"那行的按钮**是可点的**；这里改严了，并且加了图上没有的完成提示 |
