# 引用支持度校验与多轮评测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-10-23-citation-support-and-multiturn-eval-design.md` 落地三件事：引用支持度校验与持久化、批量评测单/多轮模式、多轮用例手工录入与 LLM 生成，并完成可复现的功能验收。

**Architecture:** Phase 1 在 cite 节点后增加纯函数字面校验，结果写入 `verify_report` 并随消息落库；Phase 2 在评测 runner 内统一“逐轮执行 + 逐轮 RAGAS 样本映射”，报告按题/按轮两种口径聚合；Phase 3 复用同一套“逐句原文拼接”校验，把多轮用例的录入、生成、编辑、核对、导出都接到 `eval_cases.turns`。前后端契约以 Pydantic/OpenAPI 为唯一来源，前端 `schema.d.ts` 重新生成。

**Tech Stack:** Python 3.13 / FastAPI / asyncpg / PostgreSQL / LangGraph / Pydantic v2 / pytest / React 19 / TypeScript / react-markdown / remark / Vite.

**Spec:** `docs/superpowers/specs/2026-10-23-citation-support-and-multiturn-eval-design.md`

## Global Constraints

- 迁移必须幂等；`005` 连续执行两次不报错，`eval_runs_dialogue_mode_check` 存在。列与 CHECK 分开加，CHECK 用 `DO $mig$` 按约束名补。
- 支持度判定只做字面规则：`NFKC` 后去空白/标点/符号，保留中文/字母/数字；中文二元组 + 字母/数字 token；公共片段用 4-gram 集合，不用 LCS。
- 阈值常量：`SUPPORT_UNIT_RECALL = 0.4`、`SUPPORT_MIN_UNITS = 3`、`SUPPORT_LONG_UNIT = 4`。不阻断回答，只标注。
- `citation_support_rate = supported_claims / (supported_claims + len(unsupported_citations))`，分母 0 时 `None`；编号级错误不混进这个分母。
- 多轮 RAGAS：所有 `route == "knowledge"`、未拒答、有答案且有检索上下文的轮次都送；每轮一条样本；整批跑完后一次性 `score_samples()`；`user_input` 用 `resolved_query or question`；`reference` 允许 `None`。
- 多轮评测不做滚动压缩，作为与线上的已知差异写进报告/文档。
- 标准答案校验（单轮/多轮共用）：按 `。；！？\n` 切分，不按逗号；实质分句 = 归一化后 ≥8 字，或含数字，或含长度 ≥2 的英文 token；实质分句 1~3 个；每个实质分句逐字命中窗口；所有数字在窗口里；覆盖率 ≥0.8。
- 生成提示词只影响选题与问法，不能覆盖输出 JSON、逐句原文校验、单轮无代词、多轮至少 2 轮等硬规则；`instruction` 上限 500 字，空串按 `None`。
- 多轮用例：`case_type="multi_turn"`、`question=turns[0].question`、`ground_truth=NULL`、至少 2 轮；列表不返回整段 `turns`，详情返回。
- 前端插件顺序固定：`remarkUncited → remarkUnsupported → remarkCitations`。
- 老数据兼容：没有 `messages.verify_report` 的消息不标注；没有 `eval_runs.dialogue_mode` 的 run 按 `single`；老报告字段保留。
- 不新增模型调用：支持度检查是纯字面计算；单测不真跑 LLM/GPU。

## Review Focus

1. **PDF 硬换行**：事故句在原文里是“专\n科起点本科学习”，归一化必须去掉空白后仍能判支持/不支持 —— 对应 Task 2 的 `test_pdf_line_wrap_is_normalized`。
2. **部分支持**：句子引用 `[1][2]`，只有 `[2]` 不支持时，整句不置灰但 `[2]` 角标要提示 —— Task 2/6 的 `test_partial_support_keeps_sentence_and_flags_marker`。
3. **老数据**：老消息没有 `verify_report`、老 run 没有 `dialogue_mode` 时不能 500、不能乱标 —— Task 5/7/9 的兼容测试。
4. **逐轮 RAGAS 回填**：一条多轮用例的多条样本不能覆盖同一行，必须按 `sample_map` 回填到 `(case, turn)` —— Task 8 的 `test_sample_map_fills_each_turn`。
5. **多轮用例派生与往返**：`question` 必须由 `turns[0]` 派生、列表能搜到后续轮、导出导入原样恢复 —— Task 12/13/18 的测试。

---

## File Structure

**后端**
- `backend/migrations/005_citation_and_dialogue_mode.sql`：`messages.verify_report`、`eval_runs.dialogue_mode` + CHECK。
- `backend/app/graph/nodes/citation_support.py`（新）：归一化、单元提取、支持度判定、4-gram。
- `backend/app/graph/nodes/cite.py`：`build_verify_report()` 接入支持度。
- `backend/app/schemas/chat.py`：`UnsupportedMarker`、`UnsupportedClaim`、扩展 `VerifyReport`。
- `backend/app/config/prompts/generate.txt`：硬约束第 6 条。
- `backend/app/services/conversation_service.py`、`chat_service.py`、`schemas/conversation.py`：持久化/读取 `verify_report`。
- `backend/app/schemas/eval.py`：`EvalTurn`、`dialogue_mode`、`turn_count`、报告新字段。
- `backend/app/eval/runner.py`：单/多轮执行、逐轮指标、`sample_map`。
- `backend/app/eval/report.py`：多轮按轮聚合、分母、`_verdict`。
- `backend/app/eval/generate.py`：窗口构建、逐句校验、多轮生成、`instruction`。
- `backend/app/eval/sets.py`：`turns` CRUD、详情、搜索、`case_source`。
- `backend/app/api/eval.py`：`dialogue_mode`、详情接口、生成参数、对比模式。
- `backend/app/cli.py` 不修改；`eval-multiturn` 保留。

**前端**
- `frontend/web/src/api/types.ts`、`api/eval.ts`、`api/schema.d.ts`（重新生成）。
- `frontend/web/src/markdown/uncitedAnnotation.ts`、`markdown/MarkdownAnswer.tsx`。
- `frontend/web/src/views/chat/ChatPage.tsx`。
- `frontend/web/src/views/admin/eval/EvalRunsPage.tsx`、`ReportDialog.tsx`、`EvalSetsPage.tsx`、`EvalAblationPage.tsx`。
- `frontend/web/src/index.css`。
- `frontend/web/scripts/annotation_probe/run.ts`。

**测试**
- 新增 `backend/tests/unit/test_citation_support.py`、`backend/tests/unit/test_generate_multi_turn.py`。
- 扩展 `test_cite.py`、`test_prompts.py`、`test_conversations_api.py`、`test_eval_runner.py`、`test_eval_api.py`、`test_eval_compare.py`、`test_eval_report.py`、`test_eval_sets_crud.py`、`test_eval_cases.py`、`test_eval_set_io.py`。
- `frontend/web/scripts/annotation_probe/run.ts` 增加 unsupported 用例。

**文档**
- `docs/评测与ragas.md`：逐轮 RAGAS、`sample_map`、分母、无 `reference`、未跑压缩。
- `openapi.json` 与 `frontend/web/src/api/schema.d.ts` 重新生成。

---

# Phase 1：引用治理

### Task 1: 迁移 005（引用报告 + 对话模式列）

**Files:**
- Create: `backend/migrations/005_citation_and_dialogue_mode.sql`
- Test: `backend/tests/integration/test_eval_sets_migration.py`（扩展）

**Interfaces:**
- Produces: `messages.verify_report JSONB`、`eval_runs.dialogue_mode TEXT NOT NULL DEFAULT 'single'`、约束 `eval_runs_dialogue_mode_check`。

- [ ] **Step 1: 写迁移**

```sql
-- 005_citation_and_dialogue_mode.sql
-- 两个变更放同一迁移：引用报告落库 + 评测对话模式。
-- ⚠️ CHECK 不能内联在 ADD COLUMN IF NOT EXISTS 里：列已存在时整条语句跳过，
--    约束就永远补不上。与 004 同一写法：先加列，再用 DO 块按约束名补 CHECK。

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

- [ ] **Step 2: 写失败测试（迁移幂等 + 约束存在）**

在 `test_eval_sets_migration.py` 增加：

```python
MIGRATION_005 = BACKEND_DIR / "migrations" / "005_citation_and_dialogue_mode.sql"


async def test_005_is_idempotent_and_creates_the_check():
    sql = MIGRATION_005.read_text(encoding="utf-8")
    async with db.tx() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        has_column = await conn.fetchval(
            """SELECT count(*) FROM information_schema.columns
                WHERE table_name='messages' AND column_name='verify_report'""")
        has_mode = await conn.fetchval(
            """SELECT count(*) FROM information_schema.columns
                WHERE table_name='eval_runs' AND column_name='dialogue_mode'""")
        has_check = await conn.fetchval(
            """SELECT count(*) FROM pg_constraint
                WHERE conname='eval_runs_dialogue_mode_check'
                  AND conrelid='eval_runs'::regclass""")
        default_mode = await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='eval_runs' AND column_name='dialogue_mode'")
    assert has_column == 1 and has_mode == 1 and has_check == 1
    assert "single" in default_mode
```

- [ ] **Step 3: 跑迁移测试**

Run: `cd D:\Knowledge_rag_system && uv run pytest backend/tests/integration/test_eval_sets_migration.py -q`
Expected: PASS（先确认 postgres 已起：`docker compose up -d --wait postgres`）。

- [ ] **Step 4: 执行 init-db 两次验证真实迁移链**

Run: `cd backend && uv run python -m app.cli init-db` 两次
Expected: 两次都打印 `✅ 005_citation_and_dialogue_mode.sql`，无报错。

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/005_citation_and_dialogue_mode.sql backend/tests/integration/test_eval_sets_migration.py
git commit -m "feat: 迁移引用报告与评测对话模式列"
```

---

### Task 2: 支持度校验纯函数

**Files:**
- Create: `backend/app/graph/nodes/citation_support.py`
- Create: `backend/tests/unit/test_citation_support.py`

**Interfaces:**
- Produces:
  - `normalize(text: str) -> str`
  - `content_units(text: str) -> set[str]`
  - `check_citation_support(answer: str, evidence: list[Chunk]) -> tuple[list[UnsupportedClaim], list[UnsupportedMarker], int]`
  - 返回 `(unsupported_claims, unsupported_markers, supported_claims)`
- Consumes: `app.graph.state.Chunk`、`app.schemas.chat` 中的 `UnsupportedClaim` / `UnsupportedMarker`（Task 3 定义）。

- [ ] **Step 1: 写失败测试**

`backend/tests/unit/test_citation_support.py`：

```python
from __future__ import annotations

from app.graph.nodes.citation_support import (
    check_citation_support, content_units, normalize,
)
from app.graph.state import Chunk


def _chunk(cid: str, text: str, *, name: str = "文档") -> Chunk:
    return Chunk(chunk_id=cid, text=text, document_id=cid, document_name=name)


ACCIDENT = "专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。"
DOC06 = "第三十五条 专科起点本科的毕（结）业证书中注明“专科起点本科学习”。"
DOC02 = "第二章 学位授予条件。本科毕业生符合条件者授予学士学位。"


def test_accident_sentence_is_unsupported_by_wrong_document():
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", DOC02)])
    assert [c.sentence for c in claims] == [ACCIDENT]
    assert claims[0].unsupported_markers == [1]
    assert [m.marker for m in markers] == [1]
    assert supported == 0


def test_same_sentence_is_supported_by_its_own_document():
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", DOC06)])
    assert claims == [] and markers == [] and supported == 1


def test_pdf_line_wrap_is_normalized():
    """原文“专\\n科起点本科学习”必须和一行写法视为同一句。"""
    wrapped = "第三十五条 专科起点本科的毕（结）业证书中注明“专\n科起点本科学习”。"
    claims, markers, supported = check_citation_support(ACCIDENT, [_chunk("c1", wrapped)])
    assert claims == [] and markers == [] and supported == 1


def test_number_mismatch_is_reported():
    answer = "免修成绩按 90 分记录[1]。"
    got, markers, _ = check_citation_support(
        answer, [_chunk("c1", "免修成绩按 85 分记录。")])
    assert got[0].reason == "number_mismatch"
    assert markers[0].reason == "number_mismatch"


def test_short_sentence_is_not_judged():
    """内容单元少于 3 个 → 不判，避免误标。"""
    claims, markers, supported = check_citation_support("可以申请[1]。", [_chunk("c1", DOC02)])
    assert claims == [] and markers == [] and supported == 0


def test_partial_support_flags_only_the_bad_marker():
    answer = "转专业需要提交申请表[1][2]。"
    got, markers, supported = check_citation_support(answer, [
        _chunk("c1", "转专业需要提交申请表。"),
        _chunk("c2", DOC02),
    ])
    assert got == []
    assert supported == 1
    assert [m.marker for m in markers] == [2]


def test_all_unsupported_marks_the_sentence():
    answer = "转专业需要提交申请表[1][2]。"
    got, markers, supported = check_citation_support(answer, [
        _chunk("c1", DOC02), _chunk("c2", DOC06),
    ])
    assert [c.sentence for c in got] == [answer]
    assert got[0].unsupported_markers == [1, 2]
    assert [m.marker for m in markers] == [1, 2]
    assert supported == 0


def test_offsets_slice_the_original_sentence():
    answer = "\n 转专业需要提交申请表[1]。"
    got, _, _ = check_citation_support(answer, [_chunk("c1", DOC02)])
    claim = got[0]
    assert answer[claim.char_start:claim.char_end] == claim.sentence


def test_units_and_normalization():
    assert normalize("ＡＢＣ，１２3\n") == "ABC123"
    units = content_units("转专业申请2025年")
    assert "转专" in units and "2025" in units
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:\Knowledge_rag_system && uv run pytest backend/tests/unit/test_citation_support.py -q`
Expected: FAIL（`ModuleNotFoundError` / 字段未定义）。

- [ ] **Step 3: 先扩展 chat schemas（本任务需要）**

`backend/app/schemas/chat.py` 增加：

```python
UnsupportedReason = Literal["no_overlap", "number_mismatch", "weak_overlap"]


class UnsupportedMarker(BaseModel):
    marker: int
    chunk_id: str
    reason: UnsupportedReason
    score: float


class UnsupportedClaim(BaseModel):
    sentence: str
    char_start: int
    char_end: int
    markers: list[int]
    unsupported_markers: list[int]
    reason: UnsupportedReason
    score: float
    cited_chunk_ids: list[str]


class VerifyReport(BaseModel):
    total_claims: int = 0
    cited_claims: int = 0
    supported_claims: int = 0
    citation_support_rate: float | None = None
    invalid_markers: list[InvalidMarker] = Field(default_factory=list)
    uncited_claims: list[UncitedClaim] = Field(default_factory=list)
    unsupported_citations: list[UnsupportedClaim] = Field(default_factory=list)
    unsupported_markers: list[UnsupportedMarker] = Field(default_factory=list)
```

- [ ] **Step 4: 实现 citation_support.py**

```python
"""引用支持度校验（字面规则，不调模型）。

判定只看“这句话的关键词/数字是否出现在被引片段里”，不做语义蕴含。
第一版阈值保守：内容单元少于 3 个不判；只提示，不阻断回答。

⚠️ 归一化必须去掉**所有空白**：PDF 原文会在词之间硬换行
（“专\n科起点本科学习”），不去空白会把同一句话判成不支持。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from app.graph.nodes.generate import is_conclusion_sentence
from app.graph.state import Chunk
from app.schemas.chat import UnsupportedClaim, UnsupportedMarker

SUPPORT_UNIT_RECALL = 0.4
SUPPORT_MIN_UNITS = 3
SUPPORT_LONG_UNIT = 4

_MARKER_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_RE = re.compile(r"[^。！？\n]*[。！？\n]?")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ALPHA_RE = re.compile(r"[A-Za-z]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

Reason = Literal["no_overlap", "number_mismatch", "weak_overlap"]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if ch.isalnum())


def content_units(text: str) -> set[str]:
    """中文二元组 + 英文 token（≥2）+ 数字（任意长度）。"""
    normalized = normalize(text)
    units: set[str] = set()
    for run in _CJK_RUN_RE.findall(normalized):
        units.update(run[i:i + 2] for i in range(len(run) - 1))
    for token in _ALPHA_RE.findall(normalized):
        if len(token) >= 2:
            units.add(token.lower())
    units.update(_NUMBER_RE.findall(normalized))
    return units


def _cjk_ngrams(text: str, n: int = SUPPORT_LONG_UNIT) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(normalize(text)):
        grams.update(run[i:i + n] for i in range(len(run) - n + 1))
    return grams


def _has_long_common(sentence: str, chunk: str) -> bool:
    return bool(_cjk_ngrams(sentence) & _cjk_ngrams(chunk))


def _supports(sentence: str, chunk_text: str) -> tuple[bool, float, Reason]:
    sentence_units = content_units(sentence)
    if not sentence_units:
        return False, 0.0, "no_overlap"
    chunk_units = content_units(chunk_text)
    recall = len(sentence_units & chunk_units) / len(sentence_units)
    numbers = _NUMBER_RE.findall(normalize(sentence))
    number_ok = all(n in normalize(chunk_text) for n in numbers)
    long_common = _has_long_common(sentence, chunk_text)
    supported = number_ok and (recall >= SUPPORT_UNIT_RECALL or long_common)
    if number_ok and recall < SUPPORT_UNIT_RECALL and not long_common:
        reason: Reason = "weak_overlap" if numbers else "no_overlap"
    elif not number_ok:
        reason = "number_mismatch"
    else:
        reason = "no_overlap"
    return supported, round(recall, 4), reason


def check_citation_support(
    answer: str, evidence: list[Chunk],
) -> tuple[list[UnsupportedClaim], list[UnsupportedMarker], int]:
    answer = answer or ""
    upper = len(evidence)
    claims: list[UnsupportedClaim] = []
    markers: list[UnsupportedMarker] = []
    supported_claims = 0

    for match in _SENTENCE_RE.finditer(answer):
        raw = match.group(0)
        sentence = raw.strip()
        if not sentence or not is_conclusion_sentence(sentence):
            continue
        legal = list(dict.fromkeys(
            int(m) for m in _MARKER_RE.findall(sentence) if 1 <= int(m) <= upper))
        if not legal:
            continue
        units = content_units(sentence)
        if len(units) < SUPPORT_MIN_UNITS:
            continue

        failures: list[UnsupportedMarker] = []
        best_score = 0.0
        best_reason: Reason = "no_overlap"
        any_supported = False
        for marker in legal:
            chunk = evidence[marker - 1]
            ok, score, reason = _supports(sentence, chunk.text or "")
            if ok:
                any_supported = True
            else:
                failures.append(UnsupportedMarker(
                    marker=marker, chunk_id=chunk.chunk_id, reason=reason, score=score))
            if score > best_score:
                best_score, best_reason = score, reason
        markers.extend(failures)

        if any_supported:
            supported_claims += 1
            continue

        leading = len(raw) - len(raw.lstrip())
        start = match.start() + leading
        claims.append(UnsupportedClaim(
            sentence=sentence,
            char_start=start,
            char_end=start + len(sentence),
            markers=legal,
            unsupported_markers=[m.marker for m in failures],
            reason=best_reason,
            score=best_score,
            cited_chunk_ids=[evidence[m - 1].chunk_id for m in legal],
        ))
    return claims, markers, supported_claims
```

注意：`supported = per := ...` 只是为了不让行太长；实现时写成普通赋值 `supported = number_ok and (...)`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest backend/tests/unit/test_citation_support.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/graph/nodes/citation_support.py backend/app/schemas/chat.py backend/tests/unit/test_citation_support.py
git commit -m "feat: 引用支持度字面校验"
```

---

### Task 3: cite 节点接入与单元测试

**Files:**
- Modify: `backend/app/graph/nodes/cite.py`
- Modify: `backend/tests/unit/test_cite.py`

**Interfaces:**
- Consumes: `check_citation_support()`。
- Produces: `build_verify_report()` 的新字段；`cite_node()` 的拒答路径仍返回空报告。

- [ ] **Step 1: 写失败测试**

在 `test_cite.py` 增加：

```python
from app.graph.nodes.citation_support import check_citation_support  # noqa: F401  (保证模块存在)


def test_cite_node_produces_support_fields():
    evidence = [
        _chunk("c1", doc="d1", name="文档一", text="转专业需要提交申请表。"),
        _chunk("c2", doc="d2", name="文档二", text="学位授予条件。"),
    ]
    answer = "转专业需要提交申请表[1]。另有一句没有依据的话。"
    report = build_verify_report(answer, evidence)
    assert report.supported_claims == 1
    assert report.unsupported_citations == []
    assert report.citation_support_rate == 1.0


def test_cite_node_reports_unsupported_marker_without_greying_sentence():
    evidence = [
        _chunk("c1", doc="d1", name="文档一", text="转专业需要提交申请表。"),
        _chunk("c2", doc="d2", name="文档二", text="学位授予条件。"),
    ]
    answer = "转专业需要提交申请表[1][2]。"
    report = build_verify_report(answer, evidence)
    assert [m.marker for m in report.unsupported_markers] == [2]
    assert report.unsupported_citations == []
    assert report.supported_claims == 1


async def test_cite_node_refusal_keeps_support_empty():
    state = new_state(decision="REFUSED_NO_EVIDENCE", answer="依据不足。", evidence=_evidence(2))
    out = await cite_node(state)
    assert out["verify_report"].unsupported_citations == []
    assert out["verify_report"].unsupported_markers == []
    assert out["verify_report"].citation_support_rate is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/unit/test_cite.py -q`
Expected: FAIL（新字段不存在 / 未计算）。

- [ ] **Step 3: 修改 `build_verify_report`**

在现有循环之后加：

```python
    unsupported, unsupported_markers, supported = check_citation_support(answer, evidence)
    denominator = supported + len(unsupported)
    return VerifyReport(
        total_claims=total,
        cited_claims=cited,
        supported_claims=supported,
        citation_support_rate=(round(supported / denominator, 4)
                               if denominator else None),
        invalid_markers=invalid,
        uncited_claims=uncited,
        unsupported_citations=unsupported,
        unsupported_markers=unsupported_markers,
    )
```

并在文件头 import：

```python
from app.graph.nodes.citation_support import check_citation_support
```

- [ ] **Step 4: 跑测试**

Run: `uv run pytest backend/tests/unit/test_cite.py backend/tests/unit/test_citation_support.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/graph/nodes/cite.py backend/tests/unit/test_cite.py
git commit -m "feat: cite 节点产出引用支持度报告"
```

---

### Task 4: generate.txt 硬约束第 6 条

**Files:**
- Modify: `backend/app/config/prompts/generate.txt`
- Modify: `backend/tests/unit/test_prompts.py`

- [ ] **Step 1: 写失败测试**

`GENERATE_HARD_RULES` 增加：

```python
    "历史事实不得冒用本轮编号": ["历史中的事实", "不得挂本轮"],
    "结论句必须有本轮依据": ["每个结论句的事实", "本轮证据"],
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/unit/test_prompts.py -q`
Expected: FAIL（缺少判据）。

- [ ] **Step 3: 修改 generate.txt**

在 `【硬约束】` 第 5 条后加：

```text
6. **历史中的事实只能用于理解指代与省略。** 某个事实如果本轮证据不支持，
   不得写入答案；确需提及时必须注明「上一轮回答中的内容」，且不得挂本轮 [n] 编号。
   每个结论句的事实必须能在本轮证据中找到依据。
```

- [ ] **Step 4: 跑测试**

Run: `uv run pytest backend/tests/unit/test_prompts.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/config/prompts/generate.txt backend/tests/unit/test_prompts.py
git commit -m "feat: Prompt 禁止历史事实冒用本轮引用"
```

---

### Task 5: verify_report 落库与历史读取

**Files:**
- Modify: `backend/app/services/conversation_service.py`
- Modify: `backend/app/services/chat_service.py`
- Modify: `backend/app/schemas/conversation.py`
- Modify: `backend/tests/integration/test_conversations_api.py`

**Interfaces:**
- `append_messages(conn, session_id, question, answer, citations, route, verify_report: dict | None = None)`
- `get_messages()` 返回项增加 `verify_report: dict | None`
- `MessageItem.verify_report: dict | None = None`

- [ ] **Step 1: 写失败测试**

在 `test_conversations_api.py` 的 `_mk_messages` 里给助手消息带上 `verify_report`，并增加：

```python
async def test_messages_return_stored_verify_report(client, users):
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="有校验")
    await _mk_messages(sid)
    r = await client.get(f"/api/conversations/{sid}/messages", headers=_h(a))
    assert r.status_code == 200
    report = r.json()["messages"][1]["verify_report"]
    assert report["unsupported_citations"][0]["sentence"] == "专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。"
    assert report["unsupported_markers"][0]["marker"] == 1


async def test_old_message_without_verify_report_returns_none(client, users):
    a, _ = users
    sid = uuid.uuid4().hex
    await _mk_conv(sid, a["id"], title="旧消息")
    async with db.tx() as conn:
        await conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES ($1,$2,'user','问',now())""", uuid.uuid4().hex, sid)
        await conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES ($1,$2,'assistant','答',now() + interval '1 millisecond')""",
            uuid.uuid4().hex, sid)
    r = await client.get(f"/api/conversations/{sid}/messages", headers=_h(a))
    assert r.status_code == 200
    assert r.json()["messages"][1]["verify_report"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/integration/test_conversations_api.py -q`
Expected: FAIL（响应没有该字段 / 落库没有该列）。

- [ ] **Step 3: 修改 service**

`append_messages` 签名与 SQL：

```python
async def append_messages(conn: asyncpg.Connection, session_id: str, question: str,
                          answer: str, citations: list, route: str,
                          verify_report: dict | None = None) -> None:
    ...
    await conn.execute(
        """INSERT INTO messages
             (id, conversation_id, role, content, citations, verify_report, created_at)
           VALUES ($1, $2, 'assistant', $3, $4, $5, now() + interval '1 millisecond')""",
        uuid.uuid4().hex, session_id, answer, citations or [], verify_report,
    )
```

`get_messages`：

```python
        rows = await conn.fetch(
            """SELECT id, role, content, citations, verify_report, created_at
                 FROM messages WHERE conversation_id = $1 ORDER BY created_at ASC""",
            session_id,
        )
    return [{
        ...,
        "verify_report": r["verify_report"],
    } for r in rows]
```

`chat_service._persist`：

```python
    report = final.get("verify_report")
    verify_payload = (report.model_dump() if hasattr(report, "model_dump") else report)
    ...
    await append_messages(conn, session_id, query, answer, citations, route, verify_payload)
```

- [ ] **Step 4: 修改 MessageItem**

```python
class MessageItem(BaseModel):
    ...
    citations: list = []
    verify_report: dict | None = None
    created_at: str | None = None
```

- [ ] **Step 5: 跑测试**

Run: `uv run pytest backend/tests/integration/test_conversations_api.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/conversation_service.py backend/app/services/chat_service.py backend/app/schemas/conversation.py backend/tests/integration/test_conversations_api.py
git commit -m "feat: verify_report 随消息落库并可随历史读取"
```

---

### Task 6: 前端引用灰标、角标与历史显示

**Files:**
- Modify: `frontend/web/src/api/types.ts`
- Modify: `frontend/web/src/markdown/uncitedAnnotation.ts`
- Modify: `frontend/web/src/markdown/MarkdownAnswer.tsx`
- Modify: `frontend/web/src/views/chat/ChatPage.tsx`
- Modify: `frontend/web/src/index.css`
- Modify: `frontend/web/scripts/annotation_probe/run.ts`
- Modify: `frontend/web/src/api/schema.d.ts`（Task 19 统一重生成；本任务先按后端契约更新类型）

**Interfaces:**
- `VerifyReport` 增加 `supported_claims`、`citation_support_rate`、`unsupported_citations`、`unsupported_markers`。
- `annotateTree(tree, rawText, claims, opts?: { className?: string; title?: string })`。
- `remarkUnsupported(options)`。
- `unsupportedMarkerSet(markers)`。

- [ ] **Step 1: 写 probe 失败用例**

在 `annotation_probe/run.ts` import 增加：

```ts
import {
  UNSUPPORTED_TITLE,
  remarkUnsupported,
  unsupportedMarkerSet,
} from '../../src/markdown/uncitedAnnotation'
```

增加：

```ts
probe('unsupported 句子置灰', '支持度校验失败的整句用 unsupported class', () => {
  const md = '专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。'
  const processor = unified().use(remarkParse)
    .use(remarkUnsupported, {
      rawText: md,
      claims: [{ sentence: md, char_start: 0, char_end: [...md].length }],
    })
    .use(remarkRehype).use(rehypeStringify)
  const html = String(processor.stringify(processor.runSync(processor.parse(md) as never)))
  assert.ok(html.includes('class="unsupported"'))
  assert.ok(html.includes(`title="${UNSUPPORTED_TITLE}"`))
})

probe('部分支持只影响角标', 'unsupported_markers 不含 [1]，整句不置灰', () => {
  const markers = unsupportedMarkerSet([{ marker: 2 } as never])
  assert.equal(markers.has(2), true)
  assert.equal(markers.has(1), false)
})
```

- [ ] **Step 2: 跑 probe 确认失败**

Run: `cd frontend/web && npx tsx scripts/annotation_probe/run.ts`
Expected: FAIL（`remarkUnsupported` 未导出）。

- [ ] **Step 3: 扩展 `uncitedAnnotation.ts`**

```ts
export const UNSUPPORTED_TITLE = '所引资料未包含该结论的关键内容，建议核对'

export interface UnsupportedMarker {
  marker: number
}

export function unsupportedMarkerSet(markers: UnsupportedMarker[] | undefined): Set<number> {
  return new Set((markers ?? []).map((m) => m.marker))
}

interface MarkOptions { className?: string; title?: string }

function markNode(node: MdastNode, asElement: boolean, opts: MarkOptions) {
  const className = opts.className ?? 'uncited'
  const title = opts.title ?? UNCITED_TITLE
  if (asElement) {
    node.data = { ...node.data, hName: 'span', hProperties: { className, title } }
  } else {
    node.data = {
      ...node.data,
      hProperties: { ...(node.data?.hProperties ?? {}), className, title },
    }
  }
}

function annotateNode(..., opts: MarkOptions, result: AnnotationResult) { ... markNode(marked, true, opts) ... }

export function annotateTree(
  tree: MdastNode, rawText: string, claims: UncitedClaim[],
  opts: MarkOptions = {},
): AnnotationResult { ... annotateNode(tree, rawText, ranges, opts, result) ... }

export function remarkUnsupported(options: UncitedPluginOptions & MarkOptions) {
  return (tree: MdastNode) => {
    const result = annotateTree(tree, options.rawText, options.claims ?? [], {
      className: options.className ?? 'unsupported',
      title: options.title ?? UNSUPPORTED_TITLE,
    })
    if (options.report) Object.assign(options.report, result)
  }
}
```

注意：`remarkUncited` 调用 `annotateTree` 时不要传 className，保持默认 `uncited`。

- [ ] **Step 4: 修改 `types.ts`**

```ts
export type UnsupportedReason = 'no_overlap' | 'number_mismatch' | 'weak_overlap'

export interface UnsupportedMarker {
  marker: number
  chunk_id: string
  reason: UnsupportedReason
  score: number
}

export interface UnsupportedClaim {
  sentence: string
  char_start: number
  char_end: number
  markers: number[]
  unsupported_markers: number[]
  reason: UnsupportedReason
  score: number
  cited_chunk_ids: string[]
}

export interface VerifyReport {
  total_claims: number
  cited_claims: number
  supported_claims: number
  citation_support_rate: number | null
  invalid_markers: InvalidMarker[]
  uncited_claims: UncitedClaim[]
  unsupported_citations: UnsupportedClaim[]
  unsupported_markers: UnsupportedMarker[]
}
```

- [ ] **Step 5: 修改 `MarkdownAnswer.tsx`**

关键改动：

```tsx
const uncited = verify?.uncited_claims ?? []
const unsupported = verify?.unsupported_citations ?? []
const unsupportedMarkers = useMemo(
  () => unsupportedMarkerSet(verify?.unsupported_markers), [verify])

const driftedUncited = useMemo(() => checkClaims(text, uncited).length > 0, [text, uncited])
const driftedUnsupported = useMemo(
  () => checkClaims(text, unsupported).length > 0, [text, unsupported])
const reportUnsupported = useRef<AnnotationResult>({ ... })
const [selfCheckUnsupported, setSelfCheckUnsupported] = useState(false)

const unsupportedDegraded = driftedUnsupported || selfCheckUnsupported

const plugins = useMemo<RemarkPlugins>(() => {
  const list: unknown[] = [remarkGfm]
  if (uncited.length > 0 && !degraded) {
    list.push([remarkUncited, { rawText: text, claims: uncited, report: report.current }])
  }
  if (unsupported.length > 0 && !unsupportedDegraded) {
    list.push([remarkUnsupported, {
      rawText: text, claims: unsupported, report: reportUnsupported.current,
    }])
  }
  list.push(remarkCitations)
  return list as RemarkPlugins
}, [uncited, degraded, unsupported, unsupportedDegraded, text])
```

角标：

```tsx
const isUnsupported = unsupportedMarkers.has(marker)
className={`cite-badge${invalid.has(marker) ? ' invalid' : ''}${isUnsupported ? ' unsupported' : ''}`}
title={
  invalid.has(marker)
    ? `引用了不存在的证据 [${marker}]`
    : isUnsupported
      ? `编号 [${marker}] 引用的资料未包含该句的关键内容`
      : citation ? ... : `引用 [${marker}]`
}
```

汇总条：

```tsx
{unsupportedMarkers.size > 0 && (
  <div className="unsupported-summary">
    本回答含 {unsupportedMarkers.size} 处引用与原文不符
  </div>
)}
{unsupported.length > 0 && (
  <div className="unsupported-summary">
    其中 {unsupported.length} 句的所有引用均不支持
    {unsupportedDegraded && '（定位校验未通过，未标注位置）'}
  </div>
)}
```

- [ ] **Step 6: 修改 CSS**

```css
.unsupported { text-decoration: underline wavy #b45309; }
.cite-badge.unsupported { box-shadow: 0 0 0 1px #b45309; }
.unsupported-summary {
  margin-top: 8px; font-size: 13px; color: #92400e;
  background: #fffbeb; padding: 7px 11px; border-radius: 6px;
}
```

- [ ] **Step 7: 修改 ChatPage 历史映射**

`toTurns`：

```ts
verify: (answer?.verify_report ?? null) as VerifyReport | null,
```

import 增加 `VerifyReport`。并把 `openConversation` 上方的注释改成：

```ts
/** 打开历史会话。`verify_report` 随消息落库，刷新后灰标仍显示。 */
```

- [ ] **Step 8: 跑 probe 与构建**

Run: `cd frontend/web && npx tsx scripts/annotation_probe/run.ts`
Expected: 全部通过。

Run: `cd frontend/web && npm run build`
Expected: PASS（若 `schema.d.ts` 尚未包含 `verify_report`，先手工补 `MessageItem.verify_report?: { [key: string]: unknown } | null`；Task 19 会重新生成）。

- [ ] **Step 9: Commit**

```bash
git add frontend/web/src/api/types.ts frontend/web/src/markdown/uncitedAnnotation.ts frontend/web/src/markdown/MarkdownAnswer.tsx frontend/web/src/views/chat/ChatPage.tsx frontend/web/src/index.css frontend/web/scripts/annotation_probe/run.ts frontend/web/src/api/schema.d.ts
git commit -m "feat: 前端显示引用支持度灰标与角标提示"
```

---

# Phase 2：批量评测单/多轮

### Task 7: 评测契约与 API 的 dialogue_mode

**Files:**
- Modify: `backend/app/schemas/eval.py`
- Modify: `backend/app/api/eval.py`
- Modify: `backend/tests/integration/test_eval_api.py`

**Interfaces:**
- `EvalRunRequest.dialogue_mode: Literal["single", "multi"] = "single"`
- `EvalRunSummary.dialogue_mode: str = "single"`；`EvalRunDetail` 继承。
- `eval_runs.dialogue_mode` 落库；`list_runs` / `get_run` SELECT 并返回。
- `EvalCompareRow.dialogue_mode: str = "single"`。

- [ ] **Step 1: 写失败测试**

`test_eval_api.py`：

```python
async def test_dialogue_mode_defaults_to_single(client, admin):
    s = await _new_set(client, admin)
    try:
        await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                          json={"question": "q", "ground_truth": "a"},
                          headers=admin["headers"])
        r = await client.post("/api/admin/eval/run",
                              json={"set_id": s["id"], "role": "student"},
                              headers=admin["headers"])
        assert r.status_code == 202
        row = await _run_row(r.json()["run_id"])
        assert row["dialogue_mode"] == "single"
        detail = await client.get(f"/api/admin/eval/runs/{r.json()['run_id']}",
                                  headers=admin["headers"])
        assert detail.json()["dialogue_mode"] == "single"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE set_id=$1", s["id"])
        await _cleanup_set(s["id"])


async def test_dialogue_mode_multi_is_stored_and_returned(client, admin):
    s = await _new_set(client, admin)
    try:
        await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                          json={"question": "q", "ground_truth": "a"},
                          headers=admin["headers"])
        r = await client.post("/api/admin/eval/run",
                              json={"set_id": s["id"], "role": "student",
                                    "dialogue_mode": "multi"},
                              headers=admin["headers"])
        assert r.status_code == 202
        row = await _run_row(r.json()["run_id"])
        assert row["dialogue_mode"] == "multi"
        listed = await client.get("/api/admin/eval/runs", headers=admin["headers"])
        item = next(x for x in listed.json()["items"] if x["run_id"] == r.json()["run_id"])
        assert item["dialogue_mode"] == "multi"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE set_id=$1", s["id"])
        await _cleanup_set(s["id"])


async def test_dialogue_mode_rejects_unknown_value(client, admin):
    r = await client.post("/api/admin/eval/run",
                          json={"suite": "full", "role": "student",
                                "dialogue_mode": "triple"},
                          headers=admin["headers"])
    assert r.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/integration/test_eval_api.py -q -k dialogue_mode`
Expected: FAIL。

- [ ] **Step 3: 修改 schemas**

```python
DialogueMode = Literal["single", "multi"]

class EvalRunRequest(BaseModel):
    ...
    dialogue_mode: DialogueMode = "single"

class EvalRunSummary(BaseModel):
    ...
    dialogue_mode: DialogueMode = "single"

class EvalCompareRow(BaseModel):
    ...
    dialogue_mode: DialogueMode = "single"
```

- [ ] **Step 4: 修改 API**

`start_run` INSERT：

```python
        await conn.execute(
            """INSERT INTO eval_runs (id, name, config, role, include_restricted,
                                      status, set_id, set_name, dialogue_mode)
               VALUES ($1,$2,$3,$4,$5,'pending',$6,$7,$8)""",
            run_id, payload.name, cfg, payload.role,
            1 if payload.include_restricted else 0,
            payload.set_id, set_name, payload.dialogue_mode)
```

`list_runs` SELECT 增加 `dialogue_mode`，响应字典增加 `"dialogue_mode": r["dialogue_mode"] or "single"`。

`get_run` 响应增加 `dialogue_mode=run["dialogue_mode"] or "single"`。

`compare` SELECT 增加 `dialogue_mode`，行增加 `"dialogue_mode": r["dialogue_mode"] or "single"`。

- [ ] **Step 5: 跑测试**

Run: `uv run pytest backend/tests/integration/test_eval_api.py -q -k dialogue_mode`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/eval.py backend/app/api/eval.py backend/tests/integration/test_eval_api.py
git commit -m "feat: 评测运行携带单/多轮对话模式"
```

---

### Task 8: Runner 单/多轮执行与逐轮 RAGAS 映射

**Files:**
- Modify: `backend/app/eval/runner.py`
- Modify: `backend/tests/integration/test_eval_runner.py`

**Interfaces:**
- `run_eval(run_id)` 读 `run["dialogue_mode"]`。
- 多轮结果写入 `metrics["turns"]`，每条含：
  `turn, question, resolved_query, route, route_ok, clarify_ok, resolved_anchor_hit,
  retrieval_hit, top1_doc, evidence_ids, citation_support_rate,
  unsupported_citations, unsupported_markers, answer, ragas_*`。
- `metrics["turns_expected"]`、`metrics["turns_completed"]`、`metrics["turns_with_reference"]`。
- RAGAS 回填用 `sample_map: list[tuple[int, int]]`。

- [ ] **Step 1: 写失败测试**

在 `test_eval_runner.py` 增加辅助与测试：

```python
async def _add_turn_case(conn, set_id, *, turns, question=None):
    case_id = uuid.uuid4().hex
    await conn.execute(
        """INSERT INTO eval_cases (id, set_id, question, ground_truth, case_type, suite,
                                   source, in_eval, turns)
           VALUES ($1,$2,$3,NULL,'multi_turn','full','manual',TRUE,$4)""",
        case_id, set_id, question or turns[0]["question"], turns)
    return case_id


async def test_single_mode_multi_turn_case_uses_first_turn(scratch_set, monkeypatch):
    turns = [
        {"question": "转专业需要什么条件？", "ground_truth": "第一轮答案",
         "expected_document": "转专业办法"},
        {"question": "那需要什么材料？", "ground_truth": "第二轮答案"},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    seen = []
    async def fake(state):
        seen.append(state["query"])
        return {"reranked": [Chunk(chunk_id="c1", text="片段", document_id="d1",
                                   document_name="转专业办法")],
                "evidence": [], "answer": "答", "refused": False,
                "route": "knowledge", "decision": "ANSWERED",
                "verify_report": {"total_claims": 1, "cited_claims": 1}}
    monkeypatch.setattr(runner, "_drive_to_end", fake)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="single")
    try:
        await runner.run_eval(run_id)
        assert seen == ["转专业需要什么条件？"]
        row = (await _results(run_id))[0]
        assert row["question"] == "转专业需要什么条件？"
        assert row["ground_truth"] == "第一轮答案"
        assert row["metrics"]["rank"] == 1
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_multi_mode_replays_turns_with_history_and_clarify_counts(
        scratch_set, monkeypatch):
    turns = [
        {"question": "转专业需要什么条件？", "ground_truth": "条件A",
         "expected_route": "knowledge", "should_clarify": 0},
        {"question": "那需要什么材料？", "ground_truth": "材料B",
         "expected_route": "knowledge", "should_clarify": 0,
         "resolved_must_contain_any": ["材料"]},
        {"question": "多久？", "ground_truth": "时限C",
         "expected_route": "clarify", "should_clarify": 1},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    seen = []
    async def fake(state):
        seen.append({
            "query": state["query"],
            "history": list(state.get("history") or []),
            "last_route": state.get("last_route") or "",
            "clarify_chain": state.get("clarify_chain") or 0,
            "clarify_total": state.get("clarify_total") or 0,
        })
        route = "clarify" if state["query"] == "多久？" else "knowledge"
        answer = "澄清问题" if route == "clarify" else f"答：{state['query']}"
        return {"reranked": [], "evidence": [], "answer": answer,
                "refused": False, "route": route, "decision": "ANSWERED",
                "resolved_query": state["query"], "verify_report": {}}
    monkeypatch.setattr(runner, "_drive_to_end", fake)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="multi")
    try:
        await runner.run_eval(run_id)
        assert [s["query"] for s in seen] == [t["question"] for t in turns]
        assert seen[1]["history"][0].role == "user"
        assert seen[1]["history"][1].role == "assistant"
        assert seen[1]["last_route"] == "knowledge"
        assert seen[2]["last_route"] == "knowledge"
        assert seen[2]["clarify_chain"] == 0
        row = (await _results(run_id))[0]
        assert len(row["metrics"]["turns"]) == 3
        assert row["metrics"]["turns"][2]["route"] == "clarify"
        assert row["metrics"]["turns"][2]["clarify_ok"] is True
        assert row["metrics"]["turns_expected"] == 3
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)


async def test_multi_mode_ragas_sample_map_fills_each_turn(
        scratch_set, monkeypatch):
    turns = [
        {"question": "第一问", "ground_truth": "答案一"},
        {"question": "第二问", "ground_truth": "答案二"},
    ]
    async with db.tx() as conn:
        await _add_turn_case(conn, scratch_set, turns=turns)
    async def fake(state):
        return {"reranked": [], "evidence": [Chunk(chunk_id="c1", text="片段")],
                "answer": f"答{state['query']}", "refused": False,
                "route": "knowledge", "decision": "ANSWERED",
                "resolved_query": state["query"], "verify_report": {}}
    monkeypatch.setattr(runner, "_drive_to_end", fake)

    calls = {}
    async def score(batch, *a, **kw):
        calls["batch"] = batch
        rows = [{"faithfulness": 0.1, "answer_relevancy": 0.2,
                 "context_precision": None, "context_recall": None},
                {"faithfulness": 0.3, "answer_relevancy": 0.4,
                 "context_precision": None, "context_recall": None}]
        return {"available": True, "ok": True, "rows": rows, "means": {},
                "errors": [], "ms": 0}
    monkeypatch.setattr(runner.ragas_service, "score_samples", score)
    run_id = await _make_run(set_id=scratch_set, set_name="跑测集",
                             dialogue_mode="multi")
    try:
        await runner.run_eval(run_id)
        row = (await _results(run_id))[0]
        t1, t2 = row["metrics"]["turns"]
        assert t1["ragas_faithfulness"] == 0.1
        assert t2["ragas_faithfulness"] == 0.3
        assert [s["user_input"] for s in calls["batch"]] == ["第一问", "第二问"]
        assert calls["batch"][1]["reference"] == "答案二"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/integration/test_eval_runner.py -q -k "first_turn or replays or sample_map"`
Expected: FAIL。

- [ ] **Step 3: 重构 runner**

在 `_run_eval` 读取：

```python
        dialogue_mode = run["dialogue_mode"] or "single"
```

把单题循环拆成 `_run_single_case(...)` 与 `_run_multi_case(...)`。核心代码：

```python
def _first_turn(case: dict) -> dict | None:
    turns = case.get("turns") or []
    return turns[0] if turns else None


def _expected_target(case: dict, turn: dict | None) -> tuple[set[str], str | None]:
    title = (turn or {}).get("expected_document") or None
    raw = case.get("expected_doc_ids")
    ids = set(json.loads(raw) if isinstance(raw, str) else (raw or []))
    return ids, title


def _retrieval_rank(chunks: list, expected_ids: set[str],
                    expected_title: str | None) -> int | None:
    if expected_title:
        return next((i for i, c in enumerate(chunks, 1)
                     if (c.document_name or "") == expected_title), 0) or None
    if expected_ids:
        return next((i for i, c in enumerate(chunks, 1)
                     if c.document_id in expected_ids), 0) or None
    return None


def _turn_row(final: dict, turn: dict, *, index: int,
              expected_ids: set[str], expected_title: str | None) -> dict:
    chunks = _rank_chunks(final)[:TOP_K]
    report = final.get("verify_report") or {}
    if hasattr(report, "model_dump"):
        report = report.model_dump()
    route = final.get("route") or ""
    expected_route = turn.get("expected_route")
    if expected_route is None and turn.get("should_clarify") is not None:
        expected_route = "clarify" if int(turn["should_clarify"]) else "knowledge"
    should = turn.get("should_clarify")
    anchors = turn.get("resolved_must_contain_any") or []
    resolved = final.get("resolved_query") or ""
    rank = _retrieval_rank(chunks, expected_ids, expected_title)
    return {
        "turn": index,
        "question": turn.get("question") or "",
        "resolved_query": resolved,
        "route": route,
        "route_ok": (route == expected_route) if expected_route else None,
        "clarify_ok": ((route == "clarify") == bool(int(should)))
                      if should is not None else None,
        "resolved_anchor_hit": any(a in resolved for a in anchors) if anchors else None,
        "retrieval_hit": (rank is not None) if expected_title or expected_ids else None,
        "top1_doc": (chunks[0].document_name or chunks[0].document_id) if chunks else "(空)",
        "evidence_ids": ([c.chunk_id for c in (final.get("evidence") or [])]
                         if (expected_title or expected_ids) else None),
        "citation_support_rate": report.get("citation_support_rate"),
        "unsupported_citations": len(report.get("unsupported_citations") or []),
        "unsupported_markers": len(report.get("unsupported_markers") or []),
        "answer": (final.get("answer") or "")[:2000],
    }
```

`_run_multi_case`：

```python
async def _run_multi_case(case: dict, *, run_id: str, role: str,
                          include_restricted: bool, config: dict,
                          visibility: dict, samples: list[dict],
                          sample_map: list[tuple[int, int]], result_index: int) -> dict:
    turns = case.get("turns") or []
    if not turns:
        turns = [{"question": case["question"],
                  "ground_truth": case.get("ground_truth")}]
    session_id = f"eval-{run_id[:8]}-{case['id']}"
    history: list[Message] = []
    last_route = ""
    routes: list[str] = []
    turn_rows: list[dict] = []
    first_answer = ""
    t0 = time.perf_counter()
    for i, turn in enumerate(turns, start=1):
        clarify_chain = 0
        for r in reversed(routes):
            if r != "clarify":
                break
            clarify_chain += 1
        clarify_total = sum(1 for r in routes if r == "clarify")
        state = new_state(
            query=turn["question"], session_id=session_id,
            user=UserContextLite(id="eval", role=role),
            history=list(history), last_route=last_route,
            clarify_chain=clarify_chain, clarify_total=clarify_total,
            include_restricted=include_restricted,
            eval_config=config if isinstance(config, dict) else {},
        )
        final = await _drive_to_end(state)
        expected_ids, expected_title = _expected_target(case, turn)
        row = _turn_row(final, turn, index=i, expected_ids=expected_ids,
                        expected_title=expected_title)
        turn_rows.append(row)
        route = row["route"]
        answer = final.get("answer") or ""
        if not first_answer:
            first_answer = answer
        contexts = [render_chunk_block(c, idx)
                    for idx, c in enumerate(final.get("evidence") or [], start=1)]
        reference = turn.get("ground_truth") or None
        if (route == "knowledge" and not bool(final.get("refused"))
                and answer and contexts):
            samples.append({
                "user_input": row["resolved_query"] or turn["question"],
                "response": answer, "retrieved_contexts": contexts,
                "reference": reference,
            })
            sample_map.append((result_index, i - 1))
        history.append(Message(role="user", content=turn["question"]))
        history.append(Message(role="assistant",
                               content=re.sub(r"\[\d+\]", "", answer)))
        routes.append(route)
        last_route = route
    return {
        "turns": turn_rows,
        "turns_expected": len(turns),
        "turns_completed": len(turn_rows),
        "turns_with_reference": sum(
            1 for t in turns if str(t.get("ground_truth") or "").strip()),
        "answer": first_answer,
        "question": case["question"],
        "ground_truth": case.get("ground_truth"),
        "retrieved_ids": [],
        "unauthorized_hits": 0,
        "ms": int((time.perf_counter() - t0) * 1000),
    }
```

单轮模式改造：

```python
def _single_case_state(case: dict) -> tuple[str, str | None, dict | None]:
    turn = _first_turn(case)
    if turn:
        return (turn.get("question") or case["question"],
                turn.get("ground_truth") or case.get("ground_truth"), turn)
    return case["question"], case.get("ground_truth"), None
```

`_case_metrics` 增加参数：

```python
def _case_metrics(final: dict, case: dict, visibility: dict[str, set[str]],
                  role: str, *, expected_ids: set[str] | None = None,
                  expected_title: str | None = None) -> dict:
    ...
    expected_ids = expected_ids if expected_ids is not None else _case_expected_ids(case)
    first_hit = _retrieval_rank(chunks, expected_ids, expected_title)
```

（返回 `rank` 用 `first_hit`，`recall_at_k/mrr` 逻辑照旧，只是 `None` 分支保留。）

RAGAS 回填：

```python
        if dialogue_mode == "multi":
            for pos, turn_idx in sample_map:
                row = ...  # 与 rows 同序 zip
                results[pos]["metrics"]["turns"][turn_idx].update(
                    {f"ragas_{k}": v for k, v in row.items()})
        else:
            for pos, row in zip(ragas_index, ragas.get("rows") or []):
                results[pos]["metrics"].update({f"ragas_{k}": v for k, v in row.items()})
```

单轮模式送样条件改为：

```python
            if (metrics.get("route") == "knowledge"
                    and answer and contexts and not metrics.get("refused")):
                ragas_samples.append({
                    "user_input": final.get("resolved_query") or case["question"],
                    "response": answer, "retrieved_contexts": contexts,
                    "reference": ground_truth,
                })
                ragas_index.append(len(results) - 1)
```

- [ ] **Step 4: `_aggregate` 增加 dialogue_mode 参数**

实现多轮结构指标：

```python
def _aggregate(results: list[dict], ragas: dict,
               dialogue_mode: str = "single") -> dict:
    ...
    if dialogue_mode == "multi":
        turns = [t for r in ok for t in (r["metrics"].get("turns") or [])]
        def _mean(vals):
            vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            return round(sum(vals) / len(vals), 4) if vals else None
        def _rate(key):
            vals = [t[key] for t in turns if isinstance(t.get(key), bool)]
            return _mean([1.0 if v else 0.0 for v in vals])
        summary.update({
            "turns_total": sum(int(r["metrics"].get("turns_expected") or 0) for r in ok),
            "turns_completed": sum(int(r["metrics"].get("turns_completed") or 0) for r in ok),
            "turns_with_reference": sum(int(r["metrics"].get("turns_with_reference") or 0) for r in ok),
            "route_accuracy": _rate("route_ok"),
            "clarify_accuracy": _rate("clarify_ok"),
            "retrieval_hit_rate": _rate("retrieval_hit"),
            "resolve_anchor_hit_rate": _rate("resolved_anchor_hit"),
            "citation_support_rate": _mean([t.get("citation_support_rate") for t in turns]),
            "unsupported_citations": sum(int(t.get("unsupported_citations") or 0) for t in turns),
            "unsupported_markers": sum(int(t.get("unsupported_markers") or 0) for t in turns),
            "ragas_scored_turns": sum(
                1 for t in turns if any(t.get(f"ragas_{k}") is not None
                                        for k in ("faithfulness", "answer_relevancy",
                                                  "context_precision", "context_recall"))),
            "ragas_scored_cases": sum(
                1 for r in ok if any(
                    any(t.get(f"ragas_{k}") is not None
                        for k in ("faithfulness", "answer_relevancy",
                                  "context_precision", "context_recall"))
                    for t in (r["metrics"].get("turns") or []))),
        })
        # 四张卡的均值也按轮次重算，报告页不依赖这层，但对比页会用
        summary["ragas"] = {
            k: _mean([t.get(f"ragas_{k}") for t in turns])
            for k in ("context_recall", "context_precision", "faithfulness", "answer_relevancy")
        }
        summary["multi_turn"] = {
            k: summary[k] for k in (
                "turns_total", "turns_completed", "turns_with_reference",
                "route_accuracy", "clarify_accuracy", "retrieval_hit_rate",
                "resolve_anchor_hit_rate", "citation_support_rate",
                "unsupported_citations", "unsupported_markers",
            )
        }
        summary["knowledge_count"] = sum(1 for t in turns if t.get("route") == "knowledge")
        summary["refused_count"] = sum(1 for t in turns if t.get("refused"))
        summary["refusal_rate"] = (
            round(summary["refused_count"] / summary["knowledge_count"], 4)
            if summary["knowledge_count"] else None)
        summary["answered_count"] = sum(
            1 for t in turns if t.get("route") == "knowledge" and not t.get("refused"))
    return summary
```

`turns` 行目前没有 `refused`，在 `_turn_row` 增加：

```python
        "refused": bool(final.get("refused")),
```

- [ ] **Step 5: 跑测试**

Run: `uv run pytest backend/tests/integration/test_eval_runner.py -q`
Expected: PASS（老测试也要通过）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/eval/runner.py backend/tests/integration/test_eval_runner.py
git commit -m "feat: 评测 runner 支持单轮/多轮逐轮回放"
```

---

### Task 9: 报告按题/按轮聚合与口径

**Files:**
- Modify: `backend/app/eval/report.py`
- Modify: `backend/app/schemas/eval.py`
- Modify: `backend/tests/unit/test_eval_report.py`
- Modify: `backend/tests/integration/test_eval_api.py`（报告透出）

**Interfaces:**
- `EvalReportMetric.scored_samples: int | None`、`sample_unit: Literal["case","turn"]`。
- `EvalReportCase.turns: list[dict] = []`。
- `EvalReport.dialogue_mode`、`multi_turn: dict | None`、`ragas_scored_turns: int | None`。
- `report.build(run_row, case_rows)` 返回上述字段。

- [ ] **Step 1: 写失败测试**

`test_eval_report.py`：

```python
def _turn(idx, **ragas):
    return {"turn": idx, "question": f"q{idx}", **{f"ragas_{k}": v for k, v in ragas.items()}}


def _multi_run(*, scored_turns=None, turns_total=4, turns_with_reference=2):
    metrics = {"cases": 1, "ragas_available": True, "ragas_errors": [],
               "multi_turn": {"turns_total": turns_total, "turns_completed": turns_total,
                              "turns_with_reference": turns_with_reference},
               "ragas_scored_turns": scored_turns}
    return {"metrics": metrics, "dialogue_mode": "multi"}


def test_multi_mode_cards_average_per_turn():
    case = {"case_id": "c1", "question": "q", "ground_truth": None, "answer": "a",
            "error": None, "metrics": {"turns": [
                _turn(1, faithfulness=1.0, answer_relevancy=0.8,
                      context_precision=None, context_recall=None),
                _turn(2, faithfulness=0.5, answer_relevancy=0.6,
                      context_precision=None, context_recall=None),
            ]}}
    out = report.build(_multi_run(scored_turns=2), [case])
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["faithfulness"]["score"] == 0.75
    assert cards["faithfulness"]["scored_samples"] == 2
    assert cards["faithfulness"]["sample_unit"] == "turn"
    assert cards["context_recall"]["score"] is None
    assert out["report"]["dialogue_mode"] == "multi"
    assert out["report"]["multi_turn"]["turns_total"] == 4
    assert out["report"]["ragas_scored_turns"] == 2


def test_multi_mode_per_case_row_uses_non_null_turn_mean():
    case = {"case_id": "c1", "question": "q", "ground_truth": None, "answer": "a",
            "error": None, "metrics": {"turns": [
                _turn(1, faithfulness=1.0, answer_relevancy=0.8,
                      context_precision=None, context_recall=None),
                _turn(2, faithfulness=0.5, answer_relevancy=0.6,
                      context_precision=None, context_recall=None),
            ]}}
    out = report.build(_multi_run(), [case])
    row = out["cases"][0]
    assert row["faithfulness"] == 0.75
    assert row["answer_relevancy"] == 0.7
    assert row["context_precision"] is None
    assert row["score"] is None


def test_multi_verdict_counts_turns():
    case = {"case_id": "c1", "question": "q", "ground_truth": None, "answer": "a",
            "error": None, "metrics": {"turns": [
                _turn(1, faithfulness=0.9, answer_relevancy=0.9,
                      context_precision=0.9, context_recall=0.9),
            ]}}
    out = report.build(_multi_run(scored_turns=1, turns_total=3, turns_with_reference=1),
                       [case])
    assert "轮有分" in out["report"]["verdict"]
    assert "题有分" not in out["report"]["verdict"]


def test_multi_verdict_explains_missing_context_metrics():
    case = {"case_id": "c1", "question": "q", "ground_truth": None, "answer": "a",
            "error": None, "metrics": {"turns": [
                _turn(1, faithfulness=0.9, answer_relevancy=0.9),
            ]}}
    verdict = report.build(
        _multi_run(scored_turns=1, turns_total=3, turns_with_reference=0),
        [case])["report"]["verdict"]
    assert "逐轮标准答案" in verdict
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/unit/test_eval_report.py -q`
Expected: FAIL。

- [ ] **Step 3: 修改 `report.build`**

核心：

```python
def build(run_row: dict, case_rows: list[dict]) -> dict:
    ...
    mode = run_row.get("dialogue_mode") or "single"
    multi = metrics.get("multi_turn") if mode == "multi" else None

    def _turn_values(case: dict) -> list[dict]:
        return _loads(case.get("metrics")).get("turns") or []

    rows = []
    for c in case_rows:
        metrics_c = _loads(c.get("metrics"))
        turns = metrics_c.get("turns") or []
        if mode == "multi":
            vals = {}
            for key in METRIC_KEYS:
                nums = [_number(t.get(f"ragas_{key}")) for t in turns]
                vals[key] = _mean([v for v in nums if v is not None])
        else:
            vals = _case_values(c)
        rows.append({
            ...,
            **vals,
            "turns": turns,
        })
    ...
    cards = []
    for key in METRIC_KEYS:
        if mode == "multi":
            scores = [_number(t.get(f"ragas_{key}"))
                      for r in rows for t in r["turns"] if _number(t.get(f"ragas_{key}")) is not None]
            unit = "turn"
            scored_cases = sum(1 for r in rows
                                if any(_number(t.get(f"ragas_{key}")) is not None
                                       for t in r["turns"]))
        else:
            scores = [r[key] for r in rows if r[key] is not None]
            unit = "case"
            scored_cases = len(scores)
        score = _mean(scores)
        cards.append({
            "key": key, "score": score, "threshold": THRESHOLDS[key],
            "passed": score is not None and score >= THRESHOLDS[key],
            "scored_cases": scored_cases,
            "scored_samples": len(scores),
            "sample_unit": unit,
        })
```

`_verdict` 改为：

```python
def _verdict(cards, available, errors, scored=None, cases=None,
             unit="case", multi=None) -> str:
    missing = [c["key"] for c in cards if c["score"] is None]
    if missing:
        names = "、".join(LABELS[k] for k in missing)
        context_missing = [k for k in missing if k in ("context_recall", "context_precision")]
        if unit == "turn" and context_missing and multi is not None:
            n = int(multi.get("turns_with_reference") or 0)
            return (f"{names}没算出来：这两项需要有逐轮标准答案；"
                    f"当前评测集只有 {n} 轮有答案。")
        ... 原有 available / reason / 通用分支 ...
    denom = _denominator(scored, cases, [c["scored_samples"] for c in cards], unit)
    ...
```

`_denominator` 增加 `unit` 参数，输出 `轮有分` 或 `题有分`。

`report.build` 返回：

```python
    scored_turns = _count(metrics.get("ragas_scored_turns")) if mode == "multi" else None
    return {
        "report": {
            ...,
            "dialogue_mode": mode,
            "multi_turn": multi,
            "ragas_scored_turns": scored_turns,
        },
        "cases": rows,
    }
```

`_verdict` 调用：

```python
            "verdict": _verdict(cards, available, errors, scored, cases,
                                unit="turn" if mode == "multi" else "case",
                                multi=multi),
```

其中 `scored = _count(metrics.get("ragas_scored_cases"))`，`cases = _count(metrics.get("cases"))`；多轮分母用 `multi["turns_total"]`：

```python
    if mode == "multi" and multi:
        scored = _count(metrics.get("ragas_scored_turns"))
        cases = _count(multi.get("turns_total"))
```

- [ ] **Step 4: 修改 schemas**

```python
class EvalReportMetric(BaseModel):
    ...
    scored_cases: int | None = None
    scored_samples: int | None = None
    sample_unit: Literal["case", "turn"] = "case"

class EvalReportCase(BaseModel):
    ...
    turns: list[dict] = []

class EvalReport(BaseModel):
    ...
    dialogue_mode: Literal["single", "multi"] = "single"
    multi_turn: dict | None = None
    ragas_scored_turns: int | None = None
```

- [ ] **Step 5: 跑测试**

Run: `uv run pytest backend/tests/unit/test_eval_report.py backend/tests/integration/test_eval_api.py -q`
Expected: PASS（老报告测试不变）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/eval/report.py backend/app/schemas/eval.py backend/tests/unit/test_eval_report.py backend/tests/integration/test_eval_api.py
git commit -m "feat: 评测报告支持按轮聚合与轮次分母"
```

---

### Task 10: 前端批量评测下拉、任务表与报告

**Files:**
- Modify: `frontend/web/src/api/eval.ts`
- Modify: `frontend/web/src/views/admin/eval/EvalRunsPage.tsx`
- Modify: `frontend/web/src/views/admin/eval/ReportDialog.tsx`

- [ ] **Step 1: 修改 `api/eval.ts`**

```ts
export interface StartRunBody {
  ...
  dialogue_mode?: 'single' | 'multi'
}
```

- [ ] **Step 2: EvalRunsPage**

增加 state 与下拉：

```tsx
const [dialogueMode, setDialogueMode] = useState<'single' | 'multi'>('single')
...
<label className="flex items-center gap-1">
  对话模式
  <select className="rounded border border-border bg-background px-2 py-1"
          value={dialogueMode}
          onChange={e => setDialogueMode(e.target.value as 'single' | 'multi')}>
    <option value="single">单轮</option>
    <option value="multi">多轮</option>
  </select>
</label>
...
const r = await startEvalRun({ set_id: setId, role, dialogue_mode: dialogueMode })
```

提示：

```tsx
<p className="text-xs text-muted-foreground">
  单轮：每道题只跑第一问。多轮：带 <code>turns</code> 的多轮题逐轮回放；普通单轮题不受影响。
</p>
```

任务表加“模式”列：

```tsx
<TableHead>模式</TableHead>
...
<TableCell>
  <span className={`rounded px-1.5 py-0.5 text-xs ${
    r.dialogue_mode === 'multi'
      ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300'
      : 'bg-muted text-muted-foreground'}`}>
    {r.dialogue_mode === 'multi' ? '多轮' : '单轮'}
  </span>
</TableCell>
```

- [ ] **Step 3: ReportDialog**

标题行：

```tsx
<span>对话模式：{mode === 'multi' ? '多轮' : '单轮'}</span>
```

其中：

```tsx
const mode = detail.dialogue_mode ?? report?.dialogue_mode ?? 'single'
const multi = mode === 'multi' ? report?.multi_turn : null
```

多轮结构指标行：

```tsx
{multi && (
  <div className="grid grid-cols-2 gap-2 rounded-md bg-secondary/40 p-3 text-xs sm:grid-cols-6">
    <span>路由准确率 <b>{fmtRate(multi.route_accuracy)}</b></span>
    <span>澄清准确率 <b>{fmtRate(multi.clarify_accuracy)}</b></span>
    <span>检索命中率 <b>{fmtRate(multi.retrieval_hit_rate)}</b></span>
    <span>消解锚点命中率 <b>{fmtRate(multi.resolve_anchor_hit_rate)}</b></span>
    <span>引用支持率 <b>{fmtRate(multi.citation_support_rate)}</b></span>
    <span>不支持引用 <b>{multi.unsupported_markers ?? 0}</b> 处 / <b>{multi.unsupported_citations ?? 0}</b> 句</span>
  </div>
)}
```

辅助：

```tsx
const fmtRate = (v: unknown) =>
  typeof v === 'number' ? v.toFixed(3) : '—'
```

卡片分母：

```tsx
const unit = c.sample_unit === 'turn' ? '轮' : '题'
const n = c.scored_samples ?? c.scored_cases
... + (typeof n === 'number' ? `，${n} ${unit}有分` : '')
```

`whyMissing` 增加多轮说明：

```tsx
function whyMissing(report: EvalReport | undefined): string {
  ...
  const mode = report?.dialogue_mode ?? 'single'
  const ref = report?.multi_turn?.turns_with_reference
  if (mode === 'multi' && typeof ref === 'number') {
    return `这两项需要有逐轮标准答案；当前评测集只有 ${ref} 轮有答案`
  }
  return '没有可评分的样本'
}
```

头部用例数旁：

```tsx
{typeof report?.ragas_scored_turns === 'number'
  ? `（RAGAS 打分 ${report.ragas_scored_turns} 轮）`
  : typeof report?.ragas_scored_cases === 'number'
    ? `（RAGAS 打分 ${report.ragas_scored_cases} 条）`
    : ''}
```

逐题表增加“轮数”列：

```tsx
<th className="whitespace-nowrap py-2 pr-3">轮数</th>
...
<td>{row.turns?.length ? row.turns.length : '—'}</td>
```

- [ ] **Step 4: 前端构建**

Run: `cd frontend/web && npm run build`
Expected: PASS（若类型还没重生成，先按 Task 19 生成）。

- [ ] **Step 5: Commit**

```bash
git add frontend/web/src/api/eval.ts frontend/web/src/views/admin/eval/EvalRunsPage.tsx frontend/web/src/views/admin/eval/ReportDialog.tsx
git commit -m "feat: 批量评测页支持单轮/多轮模式"
```

---

### Task 11: 对比页模式列与混排提示

**Files:**
- Modify: `backend/app/api/eval.py`（compare 指标全集）
- Modify: `backend/tests/integration/test_eval_compare.py`
- Modify: `frontend/web/src/views/admin/eval/EvalAblationPage.tsx`

- [ ] **Step 1: 写失败测试**

```python
async def test_compare_rows_carry_dialogue_mode(client, admin, run_factory):
    rid = await run_factory({**_metrics(), "multi_turn": {"turns_total": 3},
                             "citation_support_rate": 0.9})
    body = await _compare(client, admin, [rid])
    assert body["runs"][0]["dialogue_mode"] == "single"


async def test_compare_includes_new_metric_keys(client, admin, run_factory):
    rid = await run_factory({**_metrics(), "citation_support_rate": 0.9,
                             "unsupported_markers": 2, "unsupported_citations": 1,
                             "ragas_scored_turns": 4, "turns_total": 6})
    body = await _compare(client, admin, [rid])
    for key in ("citation_support_rate", "unsupported_markers",
                "unsupported_citations", "ragas_scored_turns", "turns_total"):
        assert key in body["metrics"]


async def test_compare_old_run_defaults_to_single(client, admin, run_factory):
    rid = await run_factory(_metrics())
    async with db.tx() as conn:
        await conn.execute("UPDATE eval_runs SET dialogue_mode='single' WHERE id=$1", rid)
    body = await _compare(client, admin, [rid])
    assert body["runs"][0]["dialogue_mode"] == "single"
```

- [ ] **Step 2: 修改 compare API**

SELECT 增加 `dialogue_mode`；`preferred` 增加：

```python
    preferred = ["recall_at_k", "mrr", "avg_ms", "refusal_rate",
                 "unauthorized_hits", "uncited_ratio", "citation_invalid",
                 "citation_support_rate", "unsupported_markers",
                 "unsupported_citations", "cases", "failed", "answered_count",
                 "knowledge_count", "ragas_scored_cases", "ragas_scored_turns",
                 "turns_total"]
```

行字典增加 `"dialogue_mode": r["dialogue_mode"] or "single"`。

`_aggregate` 多轮已把 `turns_total` 等放顶层（Task 8）。

- [ ] **Step 3: 修改 EvalAblationPage**

`METRIC_LABEL` 增加：

```ts
citation_support_rate: '引用支持率',
unsupported_markers: '不支持引用(处)',
unsupported_citations: '不支持句数',
ragas_scored_turns: 'RAGAS 打分轮数',
turns_total: '轮数',
```

历史轮次表与对比表增加“模式”列：

```tsx
<th>模式</th>
...
<td>{r.dialogue_mode === 'multi' ? '多轮' : '单轮'}</td>
```

对比表行：

```tsx
<td className="sticky left-32 ...">{row.dialogue_mode === 'multi' ? '多轮' : '单轮'}</td>
```

混排提示：

```tsx
{new Set((compare?.runs ?? []).map(r => r.dialogue_mode)).size > 1 && (
  <p className="mt-2 text-xs text-amber-700 dark:text-amber-500">
    单轮按题、多轮按轮，分母不同，四项分数不要直接横比。
  </p>
)}
```

- [ ] **Step 4: 跑测试与构建**

Run: `uv run pytest backend/tests/integration/test_eval_compare.py -q`
Run: `cd frontend/web && npm run build`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/eval.py backend/tests/integration/test_eval_compare.py frontend/web/src/views/admin/eval/EvalAblationPage.tsx
git commit -m "feat: 对比页显示单/多轮模式并提示分母差异"
```

---

# Phase 3：多轮用例录入、生成与核对

### Task 12: EvalTurn 契约与多轮用例 CRUD

**Files:**
- Modify: `backend/app/schemas/eval.py`
- Modify: `backend/app/eval/sets.py`
- Modify: `backend/app/api/eval.py`
- Modify: `backend/tests/integration/test_eval_sets_crud.py`
- Modify: `backend/tests/integration/test_eval_api.py`

**Interfaces:**
- `EvalTurn`（字段见 spec 7.2）。
- `EvalCaseCreate.case_type/turns`；多轮时 `question` 由 `turns[0]` 派生。
- `EvalCasePatch.turns`；多轮不能单独改 `question`。
- `EvalCaseItem.turn_count`；`EvalCaseDetail.turns`。
- `GET /api/admin/eval/cases/{id}`。
- `list_cases()` 搜索 `question ILIKE OR turns::text ILIKE`。

- [ ] **Step 1: 写失败测试**

`test_eval_sets_crud.py`：

```python
async def test_create_multi_turn_case_derives_question_and_turns(scratch_set):
    turns = [
        {"question": "转专业需要什么条件？", "ground_truth": "条件一",
         "expected_route": "knowledge", "should_clarify": 0},
        {"question": "那材料呢？", "ground_truth": "材料二",
         "expected_route": "knowledge", "should_clarify": 0},
    ]
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"], case_type="multi_turn",
                                      turns=turns)
        row = dict(await conn.fetchrow("SELECT * FROM eval_cases WHERE id=$1", case["id"]))
    assert case["question"] == "转专业需要什么条件？"
    assert row["case_type"] == "multi_turn"
    assert row["ground_truth"] is None
    assert case["turn_count"] == 2
    assert [t["question"] for t in case["turns"]] == ["转专业需要什么条件？", "那材料呢？"]


async def test_multi_turn_case_requires_two_turns(scratch_set):
    async with db.tx() as conn:
        with pytest.raises(ValueError):
            await sets.create_case(conn, scratch_set["id"], case_type="multi_turn",
                                   turns=[{"question": "只有一轮"}])


async def test_factual_case_rejects_turns(scratch_set):
    async with db.tx() as conn:
        with pytest.raises(ValueError):
            await sets.create_case(conn, scratch_set["id"], question="q",
                                   turns=[{"question": "a"}, {"question": "b"}])


async def test_patch_multi_turns_syncs_question_and_clears_gt(scratch_set):
    async with db.tx() as conn:
        case = await sets.create_case(
            conn, scratch_set["id"], case_type="multi_turn",
            turns=[{"question": "第一问", "ground_truth": "答案一"},
                   {"question": "第二问", "ground_truth": "答案二"}])
        updated = await sets.update_case(conn, case["id"], turns=[
            {"question": "改后第一问", "ground_truth": "新答案一"},
            {"question": "改后第二问", "ground_truth": "新答案二"}])
    assert updated["question"] == "改后第一问"
    assert updated["ground_truth"] is None
    assert updated["turn_count"] == 2


async def test_patch_multi_question_alone_is_rejected(scratch_set):
    async with db.tx() as conn:
        case = await sets.create_case(
            conn, scratch_set["id"], case_type="multi_turn",
            turns=[{"question": "第一问"}, {"question": "第二问"}])
        with pytest.raises(ValueError, match="turns"):
            await sets.update_case(conn, case["id"], question="单独改问题")


async def test_list_search_finds_later_turns(scratch_set):
    async with db.tx() as conn:
        await sets.create_case(
            conn, scratch_set["id"], case_type="multi_turn",
            turns=[{"question": "第一问"}, {"question": "缓考材料怎么交？"}])
        got = await sets.list_cases(conn, scratch_set["id"], q="缓考材料")
    assert got["total"] == 1
```

`test_eval_api.py`：

```python
async def test_case_detail_returns_turns(client, admin):
    s = await _new_set(client, admin)
    try:
        r = await client.post(f"/api/admin/eval/sets/{s['id']}/cases",
                              json={"case_type": "multi_turn", "turns": [
                                  {"question": "第一问"}, {"question": "第二问"}]},
                              headers=admin["headers"])
        assert r.status_code == 201, r.text
        case = r.json()
        assert case["turn_count"] == 2
        detail = await client.get(f"/api/admin/eval/cases/{case['id']}",
                                  headers=admin["headers"])
        assert [t["question"] for t in detail.json()["turns"]] == ["第一问", "第二问"]
    finally:
        await _cleanup_set(s["id"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/integration/test_eval_sets_crud.py -q -k "multi or turns or detail"`
Expected: FAIL。

- [ ] **Step 3: 扩展 schemas**

```python
class EvalTurn(BaseModel):
    question: str = Field(min_length=1)
    ground_truth: str | None = None
    expected_route: Literal["chat", "clarify", "knowledge"] | None = None
    should_clarify: Literal[0, 1] | None = None
    expected_document: str | None = None
    resolved_must_contain_any: list[str] | None = None
    note: str | None = None


class EvalCaseCreate(BaseModel):
    question: str | None = None
    ground_truth: str | None = None
    in_eval: bool = True
    note: str | None = None
    case_type: Literal["factual", "multi_turn"] = "factual"
    turns: list[EvalTurn] | None = None

    @model_validator(mode="after")
    def _derive(self):
        if self.case_type == "multi_turn":
            if not self.turns or len(self.turns) < 2:
                raise ValueError("多轮用例至少需要 2 轮")
            self.question = self.turns[0].question.strip()
            self.ground_truth = None
        else:
            if self.turns:
                raise ValueError("单轮用例不能带 turns")
            if not (self.question or "").strip():
                raise ValueError("问题不能为空")
        return self


class EvalCasePatch(BaseModel):
    question: str | None = Field(default=None, min_length=1)
    ground_truth: str | None = None
    in_eval: bool | None = None
    note: str | None = None
    turns: list[EvalTurn] | None = None


class EvalCaseItem(BaseModel):
    ...
    turn_count: int = 0


class EvalCaseDetail(EvalCaseItem):
    turns: list[EvalTurn] = []
```

- [ ] **Step 4: 修改 sets.py**

`EDITABLE_CASE_FIELDS` 增加 `"turns"`。新增校验：

```python
def _clean_turns(turns) -> list[dict]:
    if not isinstance(turns, list) or len(turns) < 2:
        raise ValueError("多轮用例至少需要 2 轮")
    out = []
    for i, t in enumerate(turns, start=1):
        data = t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t)
        q = str(data.get("question") or "").strip()
        if not q:
            raise ValueError(f"第 {i} 轮问题不能为空")
        data["question"] = q
        out.append(data)
    return out
```

`create_case`：

```python
async def create_case(conn, set_id, *, question=None, ground_truth=None,
                      in_eval=True, note=None, case_type="factual", turns=None):
    if case_type == "multi_turn":
        turns = _clean_turns(turns)
        question = turns[0]["question"]
        ground_truth = None
    else:
        if turns:
            raise ValueError("单轮用例不能带 turns")
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
    ...
    INSERT ... (case_type, turns) ...
```

`update_case` 开头：

```python
    existing = row["case_type"]
    if "turns" in fields:
        if existing != "multi_turn":
            raise ValueError("单轮用例不能改 turns")
        cleaned = _clean_turns(fields["turns"])
        edits["turns"] = cleaned
        edits["question"] = cleaned[0]["question"]
        edits["ground_truth"] = None
    elif existing == "multi_turn" and "question" in fields:
        raise ValueError("多轮用例的问题由 turns[0] 决定，请改 turns")
```

`_case_out`：

```python
    turns = row.get("turns")
    row["turn_count"] = len(turns) if isinstance(turns, list) else 0
```

`list_cases` 搜索：

```python
    if q:
        params.append(f"%{q}%")
        where.append(f"(question ILIKE ${len(params)} OR turns::text ILIKE ${len(params)})")
```

`get_case`：

```python
async def get_case(conn, case_id: str) -> dict:
    row = await conn.fetchrow("SELECT * FROM eval_cases WHERE id=$1", case_id)
    if row is None:
        raise CaseNotFound(case_id)
    return _case_out(dict(row))
```

- [ ] **Step 5: 修改 API**

- import `EvalCaseDetail`、`EvalTurn`。
- `create_case` 传 `case_type/payload.turns`，catch `ValueError` → `HTTPException(400, detail=str(e))`。
- `patch_case` 同样 catch `ValueError`。
- 新增：

```python
@router.get("/cases/{case_id}", response_model=EvalCaseDetail)
async def get_case(case_id: str, user=AdminUser) -> EvalCaseDetail:
    async with db.tx() as conn:
        try:
            row = await sets.get_case(conn, case_id)
        except sets.CaseNotFound:
            raise NotFound("用例不存在") from None
    return EvalCaseDetail(**row)
```

- [ ] **Step 6: 跑测试**

Run: `uv run pytest backend/tests/integration/test_eval_sets_crud.py backend/tests/integration/test_eval_api.py -q`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/eval.py backend/app/eval/sets.py backend/app/api/eval.py backend/tests/integration/test_eval_sets_crud.py backend/tests/integration/test_eval_api.py
git commit -m "feat: 多轮用例手工录入与详情接口"
```

---

### Task 13: 共用逐句校验与单轮生成改造

**Files:**
- Modify: `backend/app/eval/generate.py`
- Modify: `backend/tests/integration/test_eval_generate.py`

**Interfaces:**
- `split_clauses(answer: str) -> list[str]`
- `validate_ground_truth(answer: str, window_text: str) -> tuple[bool, str]`
- `ask_model_for_case()` 改用新校验；单轮 Prompt 允许 1~3 个原文分句。

- [ ] **Step 1: 写失败测试**

```python
WINDOW = ("退役复学学生可以申请免修课程。免修成绩按85分记录。"
          "退役复学后所修课程成绩可乘以1.5，最高不超过85分。学校应当审核申请材料。")


def test_validate_accepts_single_clause_and_splicing():
    assert generate.validate_ground_truth("免修成绩按85分记录。", WINDOW)[0]
    assert generate.validate_ground_truth(
        "退役复学学生可以申请免修课程；免修成绩按85分记录。", WINDOW)[0]


def test_validate_rejects_rewrites_and_numbers():
    assert not generate.validate_ground_truth(
        "退役复学学生能申请不用上课。", WINDOW)[0]
    assert not generate.validate_ground_truth(
        "免修成绩按90分记录。", WINDOW)[0]


def test_validate_rejects_connector_not_in_window():
    assert not generate.validate_ground_truth(
        "此外，免修成绩按85分记录。", WINDOW)[0]


def test_validate_rejects_more_than_three_substantive_clauses():
    answer = ("退役复学学生可以申请免修课程。免修成绩按85分记录。"
              "退役复学后所修课程成绩可乘以1.5，最高不超过85分。"
              "学校应当审核申请材料。")
    assert not generate.validate_ground_truth(answer, WINDOW)[0]


def test_validate_rejects_thin_generalization():
    assert not generate.validate_ground_truth("可以申请。", WINDOW)[0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/integration/test_eval_generate.py -q -k validate`
Expected: FAIL。

- [ ] **Step 3: 实现共用校验**

```python
_CLAUSE_RE = re.compile(r"[。；！？\n]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ALPHA_RE = re.compile(r"[A-Za-z]{2,}")


def split_clauses(answer: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_RE.split(answer or "") if c.strip()]


def _substantive(clause: str) -> bool:
    squeezed = squeeze(clause)
    return (len(squeezed) >= 8 or bool(_NUMBER_RE.search(squeezed))
            or bool(_ALPHA_RE.search(squeezed)))


def validate_ground_truth(answer: str, window_text: str) -> tuple[bool, str]:
    answer = (answer or "").strip()
    if not answer:
        return False, "标准答案为空"
    window = squeeze(window_text)
    clauses = split_clauses(answer)
    substantive = [c for c in clauses if _substantive(c)]
    if not substantive:
        return False, "没有实质分句"
    if len(substantive) > 3:
        return False, "实质分句超过 3 句"
    for clause in clauses:
        if squeeze(clause) not in window:
            return False, f"分句不是窗口原文：{clause[:30]}"
    for number in _NUMBER_RE.findall(answer):
        if number not in window:
            return False, f"数字不在原文：{number}"
    total = len(squeeze(answer)) or 1
    covered = sum(len(squeeze(c)) for c in substantive)
    if covered / total < 0.8:
        return False, "实质分句覆盖率不足 0.8"
    return True, ""
```

修改 `ask_model_for_case`：

```python
        ok, _why = validate_ground_truth(answer, chunk)
        if not (question and ok):
            continue
```

修改 `_prompt` 的硬要求 1~2：

```text
1. `ground_truth` 由 1~3 个原文分句组成；多个分句直接用“；”或“。”按原文拼接，
   不要改写、不要概括、不要添加原文没有的连接词。
2. 每个实质分句（≥8 字、含数字或含英文词）都必须是上面材料里的逐字原文，
   不要求连续；所有数字都必须出现在材料里。
```

few-shot 改成两句拼接示例，并确保示例中的每句都在示例材料里。

- [ ] **Step 4: 跑测试**

Run: `uv run pytest backend/tests/integration/test_eval_generate.py -q`
Expected: PASS（老测试也通过）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/generate.py backend/tests/integration/test_eval_generate.py
git commit -m "feat: 单轮生成改用逐句原文拼接校验"
```

---

### Task 14: 窗口构建与多轮生成

**Files:**
- Modify: `backend/app/eval/generate.py`
- Create: `backend/tests/unit/test_generate_multi_turn.py`
- Modify: `backend/tests/integration/test_eval_cases.py`（生成接口/入库）
- Modify: `backend/app/schemas/eval.py`
- Modify: `backend/app/api/eval.py`

**Interfaces:**
- `build_windows(chunks, window, count, normalized_text=None) -> list[dict]`
- `ask_model_for_multi(window_text, title, target_turns, instruction=None) -> dict | None`
- `generate_cases(..., mode="single", turns=3, window=3, instruction=None)`
- `EvalGenerateRequest` 新字段；`EvalGenerateCaseOut.turn_count`。

- [ ] **Step 1: 写窗口/校验失败测试**

`test_generate_multi_turn.py`：

```python
from app.eval import generate

CHUNKS = [
    {"chunk_id": f"c{i}", "text": f"第{i}段原文内容。",
     "metadata": {"chunk_index": i, "page": i + 1,
                  "char_start": i * 10, "char_end": (i + 1) * 10}}
    for i in range(5)
]
TEXT = "第0段原文内容。第1段原文内容。第2段原文内容。第3段原文内容。第4段原文内容。"


def test_windows_are_adjacent_and_merge_by_offsets():
    windows = generate.build_windows(CHUNKS, 3, 2, normalized_text=TEXT)
    assert len(windows) == 2
    assert windows[0]["text"] == TEXT[0:30]
    assert windows[1]["text"] == TEXT[20:50]


def test_windows_overlap_when_not_enough_starts():
    windows = generate.build_windows(CHUNKS, 3, 5, normalized_text=TEXT)
    assert len(windows) == 5
    assert all(w["text"] for w in windows)


def test_window_falls_back_to_overlap_merge_without_offsets():
    chunks = [{"chunk_id": "c1", "text": "ABCDE", "metadata": {"chunk_index": 0}},
              {"chunk_id": "c2", "text": "CDEFG", "metadata": {"chunk_index": 1}}]
    windows = generate.build_windows(chunks, 2, 1)
    assert windows[0]["text"] == "ABCDEFG"


def test_validate_multi_accepts_two_to_four_turns():
    data = {"turns": [
        {"question": "退役复学可以申请什么？", "ground_truth": "退役复学学生可以申请免修课程。"},
        {"question": "那成绩怎么记？", "ground_truth": "免修成绩按85分记录。"},
    ]}
    got = generate.validate_multi_dialogue(data, (
        "退役复学学生可以申请免修课程。免修成绩按85分记录。"), "某办法", 2)
    assert got is not None
    assert got["turns"][0]["expected_route"] == "knowledge"
    assert got["turns"][0]["should_clarify"] == 0


def test_validate_multi_rejects_first_turn_pronoun():
    data = {"turns": [
        {"question": "这个可以申请什么？", "ground_truth": "退役复学学生可以申请免修课程。"},
        {"question": "那成绩怎么记？", "ground_truth": "免修成绩按85分记录。"},
    ]}
    assert generate.validate_multi_dialogue(
        data, "退役复学学生可以申请免修课程。免修成绩按85分记录。", "某办法", 2) is None


def test_validate_multi_rejects_duplicate_answers():
    data = {"turns": [
        {"question": "第一问", "ground_truth": "免修成绩按85分记录。"},
        {"question": "第二问", "ground_truth": "免修成绩按85分记录。"},
    ]}
    assert generate.validate_multi_dialogue(
        data, "免修成绩按85分记录。", "某办法", 2) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest backend/tests/unit/test_generate_multi_turn.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现窗口**

```python
def _merge_by_overlap(texts: list[str]) -> str:
    out = ""
    for text in texts:
        text = text or ""
        if not out:
            out = text
            continue
        max_k = min(len(out), len(text))
        for k in range(max_k, 0, -1):
            if out.endswith(text[:k]):
                out += text[k:]
                break
        else:
            out += text
    return out


def build_windows(chunks: list[dict], window: int, count: int,
                  normalized_text: str | None = None) -> list[dict]:
    chunks = sorted(chunks, key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
    window = max(2, min(3, int(window or 3)))
    if not chunks:
        return []
    if len(chunks) < window:
        starts = [0]
    else:
        max_start = len(chunks) - window
        if max_start + 1 >= count:
            starts = [int(i * max_start / max(1, count - 1)) for i in range(count)]
        else:
            starts = [int(i * max_start / max(1, count - 1)) for i in range(count)]
    out = []
    for start in starts:
        picked = chunks[start:start + window]
        metas = [c.get("metadata") or {} for c in picked]
        text = ""
        if (normalized_text and all(
                isinstance(m.get("char_start"), int) and isinstance(m.get("char_end"), int)
                and 0 <= m["char_start"] < m["char_end"] <= len(normalized_text)
                for m in metas)):
            lo = min(m["char_start"] for m in metas)
            hi = max(m["char_end"] for m in metas)
            text = normalized_text[lo:hi]
        if not text:
            text = _merge_by_overlap([c.get("text") or "" for c in picked])
        out.append({"chunks": picked, "text": text.strip()})
    return out
```

- [ ] **Step 4: 实现多轮 Prompt 与校验**

```python
MULTI_TIMEOUT = 180.0
MULTI_CONCURRENCY = 3
MULTI_MAX_TOKENS = 2000


def _multi_prompt(window: str, title: str, target_turns: int,
                  instruction: str | None) -> str:
    extra = (f"\n【管理员额外要求】\n{instruction}\n"
             "额外要求只影响选题与问法；与下面的硬性要求冲突时，一律以硬性要求为准。\n"
             if instruction else "")
    return (
        f"下面是《{title}》里连续的一段材料：\n\n\"\"\"\n{window}\n\"\"\"\n\n"
        f"{extra}"
        f"请围绕这段材料生成 {target_turns} 轮连续追问（2~4 轮都接受）。\n"
        "严格要求：\n"
        "1. 只输出 JSON：{\"turns\":[{\"question\":\"...\",\"ground_truth\":\"...\"}]}\n"
        "2. 第一轮必须自足，问句里**不许出现代词**。\n"
        "3. 后续轮必须使用代词或省略（那 / 它 / 这个 / 该 / 其 / 多久……）。\n"
        "4. 每轮 ground_truth 由 1~3 个原文分句组成；多个分句按原文用“；”或“。”拼接，"
        "不要改写、概括或添加原文没有的连接词；每个实质分句都必须是窗口材料里的逐字原文。\n"
        "5. 各轮答案不能重复，至少覆盖 2 个不同事实。\n"
        "6. 不许跨出窗口材料，不许用常识补全。\n"
        "7. 轮数 2~4，默认 3。\n\n"
        "示例：\n"
        "{\"turns\":[\n"
        " {\"question\":\"退役复学可以申请什么？\","
        "\"ground_truth\":\"退役复学学生可以申请免修课程。\"},\n"
        " {\"question\":\"那成绩怎么记？\","
        "\"ground_truth\":\"免修成绩按85分记录。\"}\n"
        "]}\n"
    )


def validate_multi_dialogue(data: dict, window_text: str, title: str,
                            target_turns: int) -> dict | None:
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list) or not (2 <= len(turns) <= 4):
        return None
    questions: list[str] = []
    answers: list[str] = []
    cleaned = []
    for i, raw in enumerate(turns, start=1):
        if not isinstance(raw, dict):
            return None
        q = str(raw.get("question") or "").strip()
        gt = str(raw.get("ground_truth") or "").strip()
        if not q or not gt or q in questions:
            return None
        if i == 1 and has_pronoun(q):
            return None
        if not validate_ground_truth(gt, window_text)[0]:
            return None
        squeezed = squeeze(gt)
        if squeezed in answers:
            return None
        questions.append(q)
        answers.append(squeezed)
        cleaned.append({
            "question": q,
            "ground_truth": gt,
            "expected_route": "knowledge",
            "should_clarify": 0,
            "expected_document": title,
        })
    return {"turns": cleaned}


async def ask_model_for_multi(window_text: str, title: str, target_turns: int,
                              instruction: str | None = None) -> dict | None:
    prompt = _multi_prompt(window_text, title, target_turns, instruction)
    for _ in range(MAX_ATTEMPTS):
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": prompt}],
                timeout=90, max_tokens=MULTI_MAX_TOKENS)
        except Exception:  # noqa: BLE001
            continue
        got = validate_multi_dialogue(data, window_text, title, target_turns)
        if got:
            return got
    return None
```

- [ ] **Step 5: `generate_cases` 分支**

```python
async def generate_cases(set_id, document_id, count, *, mode="single",
                         turns=3, window=3, instruction=None,
                         timeout=None):
    count = max(1, min(MAX_COUNT, int(count or DEFAULT_COUNT)))
    mode = "multi" if mode == "multi" else "single"
    turns = max(2, min(4, int(turns or 3)))
    window = max(2, min(3, int(window or 3)))
    instruction = (instruction or "").strip()[:500] or None
    ...
    if mode == "multi":
        windows = build_windows(chunks, window, count, normalized_text)
        if not windows:
            return _result(count, [], False, "这份文档没有可用的相邻片段")
        sem = asyncio.Semaphore(MULTI_CONCURRENCY)
        done: dict[int, dict] = {}
        async def _one_multi(idx, win):
            async with sem:
                got = await ask_model_for_multi(win["text"], title, turns, instruction)
            if got:
                done[idx] = {"window": win, "turns": got["turns"]}
        tasks = [asyncio.create_task(_one_multi(i, w))
                 for i, w in enumerate(windows)]
        _, pending = await asyncio.wait(
            tasks, timeout=timeout if timeout is not None else MULTI_TIMEOUT)
        ...
    else:
        ... 原单轮逻辑，传 instruction or FACTUAL_INSTRUCTION ...
```

`_insert_case` 分叉：

```python
async def _insert_multi_case(conn, set_id, document_id, document, item):
    first = item["window"]["chunks"][0]
    meta = first.get("metadata") or {}
    case_id = uuid.uuid4().hex
    turns = item["turns"]
    await conn.execute(
        """INSERT INTO eval_cases
             (id, set_id, question, ground_truth, case_type, suite,
              expected_doc_ids, expected_chunk_ids, turns, visible_roles,
              expected_route, should_clarify, source, in_eval, note,
              source_document_id, source_chunk_id, source_page, source_snippet)
           VALUES ($1,$2,$3,NULL,'multi_turn','full',$4,NULL,$5,NULL,
                   'knowledge',0,'generated',TRUE,$6,$7,$8,$9,$10)""",
        case_id, set_id, turns[0]["question"], [document_id], turns,
        (f"生成要求：{item['instruction'][:200]}"
         if item.get("instruction") else None),
        document_id, first.get("chunk_id"), meta.get("page"),
        item["window"]["text"])
    return {"id": case_id, "question": turns[0]["question"],
            "ground_truth": None, "turn_count": len(turns)}
```

单轮 `_insert_case` 增加 `note` 与 `turn_count: 1`。

- [ ] **Step 6: schemas + API**

```python
class EvalGenerateRequest(BaseModel):
    document_id: str
    count: int = 5
    mode: Literal["single", "multi"] = "single"
    turns: int = Field(default=3, ge=2, le=4)
    window: int = Field(default=3, ge=2, le=3)
    instruction: str | None = Field(default=None, max_length=500)


class EvalGenerateCaseOut(BaseModel):
    id: str
    question: str
    ground_truth: str | None = None
    turn_count: int = 1
```

API：

```python
stats = await generate.generate_cases(
    set_id, payload.document_id, payload.count,
    mode=payload.mode, turns=payload.turns, window=payload.window,
    instruction=payload.instruction)
```

- [ ] **Step 7: 扩展集成测试**

在 `test_eval_cases.py` 增加：假模型返回两轮合法对话 → 入库 `case_type/turns/ground_truth NULL/expected_doc_ids`；失败重试不入库；`instruction` 出现在 Prompt 与 `note`；`turn_count` 正确。

- [ ] **Step 8: 跑测试**

Run: `uv run pytest backend/tests/unit/test_generate_multi_turn.py backend/tests/integration/test_eval_generate.py backend/tests/integration/test_eval_cases.py -q`
Expected: PASS。

- [ ] **Step 9: Commit**

```bash
git add backend/app/eval/generate.py backend/tests/unit/test_generate_multi_turn.py backend/tests/integration/test_eval_cases.py backend/app/schemas/eval.py backend/app/api/eval.py
git commit -m "feat: 相邻 chunk 窗口生成多轮用例"
```

---

### Task 15: 多轮来源核对、导出导入与搜索

**Files:**
- Modify: `backend/app/eval/sets.py`
- Modify: `backend/app/schemas/eval.py`
- Modify: `backend/tests/integration/test_eval_set_io.py`
- Modify: `backend/tests/integration/test_eval_sets_crud.py`

**Interfaces:**
- `EvalCaseSource.turns: list[EvalTurn]`、`turn_highlights: list[list[int] | None]`。
- `sets.case_source()` 对每轮 `ground_truth` 调 `locate_highlight`。
- 多轮导出/导入 round-trip 已有 `turns`/`case_type`，本任务补测试与逐轮高亮。

- [ ] **Step 1: 写失败测试**

```python
async def test_case_source_returns_turn_highlights(scratch_set):
    snippet = "退役复学学生可以申请免修课程。免修成绩按85分记录。"
    turns = [
        {"question": "第一问", "ground_truth": "退役复学学生可以申请免修课程。"},
        {"question": "第二问", "ground_truth": "免修成绩按85分记录。"},
    ]
    async with db.tx() as conn:
        case = await sets.create_case(conn, scratch_set["id"],
                                      case_type="multi_turn", turns=turns)
        await conn.execute(
            "UPDATE eval_cases SET source_snippet=$2, source='generated', "
            "source_page=3, source_chunk_id='c1', source_document_id=NULL WHERE id=$1",
            case["id"], snippet)
        payload = await sets.case_source(conn, case["id"])
    assert [t["question"] for t in payload["turns"]] == ["第一问", "第二问"]
    assert len(payload["turn_highlights"]) == 2
    for span, turn in zip(payload["turn_highlights"], turns):
        start, end = span
        assert snippet[start:end] == turn["ground_truth"]
```

`test_eval_set_io.py` 增加：

```python
async def test_multi_turn_roundtrip_keeps_turns_and_case_type(scratch_set):
    turns = [{"question": "第一问", "ground_truth": "答案一"},
             {"question": "第二问", "ground_truth": "答案二"}]
    async with db.tx() as conn:
        await _insert_case(conn, scratch_set, case_type="multi_turn",
                           ground_truth=None, turns=turns)
        payload = await sets.export_payload(conn, SET_NAME)
        await conn.execute("UPDATE eval_cases SET turns=NULL, case_type='refusal',"
                           " ground_truth='脏' WHERE set_id=$1", scratch_set)
        await sets.import_payload(conn, payload)
        row = await conn.fetchrow(
            "SELECT case_type, turns, ground_truth FROM eval_cases WHERE set_id=$1",
            scratch_set)
    assert row["case_type"] == "multi_turn"
    assert row["turns"] == turns
    assert row["ground_truth"] is None
```

- [ ] **Step 2: 修改 `case_source`**

```python
    turns = _loads(row.get("turns")) or []
    return {
        ...,
        "turns": turns,
        "turn_highlights": [
            locate_highlight(snippet, (t or {}).get("ground_truth"))
            for t in turns
        ],
        "highlight": locate_highlight(snippet, row.get("ground_truth")),
    }
```

- [ ] **Step 3: 修改 schema**

```python
class EvalCaseSource(BaseModel):
    ...
    highlight: list[int] | None = None
    turns: list[EvalTurn] = []
    turn_highlights: list[list[int] | None] = []
```

- [ ] **Step 4: 跑测试**

Run: `uv run pytest backend/tests/integration/test_eval_sets_crud.py backend/tests/integration/test_eval_set_io.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/sets.py backend/app/schemas/eval.py backend/tests/integration/test_eval_sets_crud.py backend/tests/integration/test_eval_set_io.py
git commit -m "feat: 多轮用例来源高亮与导出往返"
```

---

### Task 16: 前端手工录入与列表

**Files:**
- Modify: `frontend/web/src/api/eval.ts`
- Modify: `frontend/web/src/views/admin/eval/EvalSetsPage.tsx`

- [ ] **Step 1: 修改 `api/eval.ts`**

```ts
export type EvalCaseDetail = S['EvalCaseDetail']

export const getEvalCase = (caseId: string) =>
  request<EvalCaseDetail>(`/admin/eval/cases/${caseId}`)

export const createEvalCase = (
  setId: string,
  body: {
    question?: string; ground_truth?: string | null; in_eval?: boolean;
    note?: string | null; case_type?: 'factual' | 'multi_turn';
    turns?: Array<{
      question: string; ground_truth?: string | null;
      expected_route?: 'chat' | 'clarify' | 'knowledge' | null;
      should_clarify?: 0 | 1 | null;
      expected_document?: string | null;
      resolved_must_contain_any?: string[] | null;
      note?: string | null;
    }> | null;
  },
) => request<EvalCaseItem>(`/admin/eval/sets/${setId}/cases`, {
  method: 'POST', body: JSON.stringify(body),
})

export const patchEvalCase = (
  caseId: string,
  body: {
    question?: string; ground_truth?: string | null; in_eval?: boolean;
    note?: string | null; turns?: Array<{ question: string; ground_truth?: string | null }> | null;
  },
) => request<EvalCaseItem>(`/admin/eval/cases/${caseId}`, {
  method: 'PATCH', body: JSON.stringify(body),
})
```

- [ ] **Step 2: 列表显示轮数**

问题列：

```tsx
<TableCell className="max-w-md whitespace-normal! pl-3">
  {c.question}
  {(c.turn_count ?? 0) >= 2 && (
    <span className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs text-blue-800
                     dark:bg-blue-900/40 dark:text-blue-300">
      多轮 {c.turn_count} 轮
    </span>
  )}
</TableCell>
```

标准答案列：

```tsx
{(c.turn_count ?? 0) >= 2
  ? `逐轮答案（${c.turn_count} 轮）`
  : (c.ground_truth || '—')}
```

- [ ] **Step 3: CaseDialog 支持单/多轮**

状态：

```tsx
const [caseType, setCaseType] = useState<'factual' | 'multi_turn'>(
  (state.item?.turn_count ?? 0) >= 2 ? 'multi_turn' : 'factual')
const [turns, setTurns] = useState<{ question: string; ground_truth: string }[]>([])
const [loading, setLoading] = useState(false)
```

编辑时拉详情：

```tsx
useEffect(() => {
  if (!editing || !state.item) return
  setLoading(true)
  getEvalCase(state.item.id)
    .then((d) => {
      setCaseType(d.case_type === 'multi_turn' ? 'multi_turn' : 'factual')
      if (d.turns?.length) {
        setTurns(d.turns.map((t) => ({
          question: t.question, ground_truth: t.ground_truth ?? '',
        })))
      }
    })
    .catch((e: Error) => setErr(e.message))
    .finally(() => setLoading(false))
}, [editing, state.item])
```

新建多轮默认两行：

```tsx
const switchType = (t: 'factual' | 'multi_turn') => {
  setCaseType(t)
  if (t === 'multi_turn' && turns.length < 2) {
    setTurns([{ question: '', ground_truth: '' }, { question: '', ground_truth: '' }])
  }
}
```

轮次表 UI：

```tsx
{caseType === 'multi_turn' && (
  <div className="space-y-2">
    <div className="flex items-center justify-between">
      <Label>轮次（至少 2 轮）</Label>
      <Button variant="outline" size="xs"
              onClick={() => setTurns(v => [...v, { question: '', ground_truth: '' }])}>
        增加一轮
      </Button>
    </div>
    {turns.map((t, i) => (
      <div key={i} className="space-y-1 rounded-md border border-border p-2">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>第 {i + 1} 轮</span>
          {turns.length > 2 && (
            <button type="button" className="link"
                    onClick={() => setTurns(v => v.filter((_, j) => j !== i))}>
              删除
            </button>
          )}
        </div>
        <Textarea rows={2} placeholder="问题" value={t.question}
                  onChange={e => setTurns(v => v.map((x, j) =>
                    j === i ? { ...x, question: e.target.value } : x))} />
        <Textarea rows={2} placeholder="标准答案（可为空）" value={t.ground_truth}
                  onChange={e => setTurns(v => v.map((x, j) =>
                    j === i ? { ...x, ground_truth: e.target.value } : x))} />
        {!t.ground_truth.trim() && (
          <p className="text-xs text-amber-600">
            第 {i + 1} 轮没有标准答案，该轮上下文指标不会计入
          </p>
        )}
      </div>
    ))}
  </div>
)}
```

提交：

```tsx
const submit = async () => {
  setBusy(true); setErr('')
  try {
    if (caseType === 'multi_turn') {
      if (turns.length < 2) throw new Error('多轮用例至少需要 2 轮')
      if (turns.some(t => !t.question.trim())) throw new Error('每一轮都要填问题')
      const payload = {
        in_eval: inEval,
        note: note || null,
        turns: turns.map(t => ({
          question: t.question.trim(),
          ground_truth: t.ground_truth || null,
        })),
      }
      if (editing && state.item) await patchEvalCase(state.item.id, payload)
      else await createEvalCase(setId, { ...payload, case_type: 'multi_turn' })
    } else {
      if (!question.trim()) throw new Error('问题不能为空')
      const payload = {
        question: question.trim(), ground_truth: groundTruth || null,
        in_eval: inEval, note: note || null,
      }
      if (editing && state.item) {
        await patchEvalCase(state.item.id, payload)
      } else {
        await createEvalCase(setId, { ...payload, case_type: 'factual' })
      }
    }
    onDone(editing ? '已保存' : '已录入一条用例')
  } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
}
```

编辑多轮时不要显示单轮问题/答案输入框；显示：

```tsx
{editing ? (
  <span className="rounded bg-muted px-2 py-1 text-xs">
    {caseType === 'multi_turn' ? '多轮用例（类型不可修改）' : '单轮用例'}
  </span>
) : (
  <div className="flex gap-2">
    <Button size="sm" variant={caseType === 'factual' ? 'default' : 'outline'}
            onClick={() => switchType('factual')}>单轮</Button>
    <Button size="sm" variant={caseType === 'multi_turn' ? 'default' : 'outline'}
            onClick={() => switchType('multi_turn')}>多轮</Button>
  </div>
)}
```

- [ ] **Step 4: 构建**

Run: `cd frontend/web && npm run build`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend/web/src/api/eval.ts frontend/web/src/views/admin/eval/EvalSetsPage.tsx
git commit -m "feat: 评测集页支持多轮用例手工录入与编辑"
```

---

### Task 17: 前端生成弹窗与核对原文

**Files:**
- Modify: `frontend/web/src/views/admin/eval/EvalSetsPage.tsx`

- [ ] **Step 1: GenerateDialog 增加模式与提示词**

状态：

```tsx
const [mode, setMode] = useState<'single' | 'multi'>('single')
const [instruction, setInstruction] = useState('')
const [turns, setTurns] = useState(3)
const [windowSize, setWindowSize] = useState(3)
```

配置 UI：

```tsx
<div className="flex gap-2">
  <Button size="sm" variant={mode === 'single' ? 'default' : 'outline'}
          onClick={() => setMode('single')}>单轮</Button>
  <Button size="sm" variant={mode === 'multi' ? 'default' : 'outline'}
          onClick={() => setMode('multi')}>多轮</Button>
</div>
<div className="space-y-1">
  <Label>生成提示词（可选）</Label>
  <Textarea rows={3} maxLength={500} value={instruction}
            placeholder={mode === 'single'
              ? '只出条件/时限类事实题，问法贴近学生口语'
              : '围绕转专业的申请条件与材料，生成连续追问'}
            onChange={e => setInstruction(e.target.value)} />
  <p className="text-xs text-muted-foreground">
    {instruction.length}/500 · 只影响选题与问法，不会放宽逐句原文校验
  </p>
</div>
{mode === 'multi' && (
  <div className="grid grid-cols-2 gap-3">
    <div className="space-y-1">
      <Label>每例轮数（2~4）</Label>
      <Input type="number" min={2} max={4} value={turns}
             onChange={e => setTurns(Number(e.target.value))} />
    </div>
    <div className="space-y-1">
      <Label>窗口大小（2~3 个 chunk）</Label>
      <Input type="number" min={2} max={3} value={windowSize}
             onChange={e => setWindowSize(Number(e.target.value))} />
    </div>
  </div>
)}
<p className="text-xs text-muted-foreground">
  {mode === 'multi'
    ? '多轮生成按相邻 chunk 窗口出题，耗时更长；标准答案严格取自窗口原文。'
    : '单轮生成的标准答案同样要求逐句原文，最多 3 个分句。'}
</p>
```

提交：

```tsx
const result = await generateEvalCases(setId, {
  document_id: docId, count: requested,
  mode, instruction: instruction.trim() || null,
  ...(mode === 'multi' ? { turns, window: windowSize } : {}),
})
```

结果列表显示：

```tsx
<p className="font-medium">
  {i + 1}. {c.question}
  {(c.turn_count ?? 1) > 1 && (
    <span className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs text-blue-800">
      多轮 {c.turn_count} 轮
    </span>
  )}
</p>
{!(c.turn_count && c.turn_count > 1) && (
  <p className="mt-0.5 text-xs text-muted-foreground">
    标准答案：{c.ground_truth || '—'}
  </p>
)}
```

- [ ] **Step 2: SourceDialog 支持逐轮答案与高亮**

```tsx
const turns = payload.turns ?? []
const turnHighlights = payload.turn_highlights ?? []
...
{turns.length > 0 && (
  <div>
    <p className="font-semibold">逐轮答案</p>
    <div className="mt-1 space-y-2">
      {turns.map((t, i) => (
        <div key={i} className="rounded-md border border-border p-2 text-sm">
          <p className="font-medium">第 {i + 1} 轮：{t.question}</p>
          <p className="mt-1 text-muted-foreground">{t.ground_truth || '—'}</p>
          {turnHighlights[i] && (
            <p className="mt-1 text-xs text-muted-foreground">
              高亮：{snippet.slice(turnHighlights[i]![0], turnHighlights[i]![1])}
            </p>
          )}
        </div>
      ))}
    </div>
  </div>
)}
```

来源片段高亮：保留现有单轮逻辑；多轮时 `start` 用第一轮高亮（`turnHighlights[0]`）或不高亮，逐轮高亮已在上面展示。为满足“逐轮高亮来自 turn_highlights”，上面已用。

- [ ] **Step 3: 构建**

Run: `cd frontend/web && npm run build`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add frontend/web/src/views/admin/eval/EvalSetsPage.tsx
git commit -m "feat: 生成弹窗支持多轮与自定义提示词，核对原文显示逐轮答案"
```

---

### Task 18: 文档更新

**Files:**
- Modify: `docs/评测与ragas.md`

- [ ] **Step 1: 增加“多轮评测”一节**

写明：
- 单轮只跑 `turns[0]`；多轮逐轮回放，累加 history/last_route/clarify_chain/clarify_total。
- 评测不做滚动压缩，这是与线上的已知差异。
- `sample_map` 把每条样本回填到 `(case, turn)`；`ragas_scored_turns` 是轮次分母。
- 有 `reference` 四项齐全；无 `reference` 只有忠实度/答案相关性，上下文两项 `null` 且不进入均值分母。
- 报告里“题有分/轮有分”的区别，单/多轮混排不能直接横比。
- 自定义生成提示词只影响选题与问法，逐句原文校验不放宽。

- [ ] **Step 2: 检查文档**

Run: `rg -n "多轮|sample_map|ragas_scored_turns|逐轮" docs/评测与ragas.md`
Expected: 能看到新增段落。

- [ ] **Step 3: Commit**

```bash
git add docs/评测与ragas.md
git commit -m "docs: 补充多轮评测与逐轮 RAGAS 口径"
```

---

### Task 19: 重新生成 OpenAPI 与前端类型

**Files:**
- Modify: `openapi.json`
- Modify: `frontend/web/src/api/schema.d.ts`

- [ ] **Step 1: 导出 openapi.json**

在仓库根用当前代码生成（不启动服务亦可）：

```bash
cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))" > ../openapi.json
```

- [ ] **Step 2: 生成前端类型**

```bash
cd frontend/web && npx openapi-typescript ../../openapi.json -o src/api/schema.d.ts
```

- [ ] **Step 3: 检查关键类型**

Run: `rg -n "UnsupportedClaim|unsupported_markers|dialogue_mode|turn_count|turns|EvalCaseDetail|Multi" frontend/web/src/api/schema.d.ts | head -40`
Expected: 新字段都存在。

- [ ] **Step 4: 构建**

Run: `cd frontend/web && npm run build`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add openapi.json frontend/web/src/api/schema.d.ts
git commit -m "chore: 重新生成 OpenAPI 与前端类型"
```

---

# 验收与回归

### Task 20: 端到端功能验收

**Files:**
- Create: `backend/tests/integration/test_citation_acceptance.py`（事故复现 + 历史落库）
- Create: `backend/tests/integration/test_eval_multiturn_acceptance.py`（单/多轮评测 + 多轮生成入库）
- Modify: `frontend/web/scripts/annotation_probe/run.ts`（已在 Task 6 覆盖）

- [ ] **Step 1: 事故复现（端到端）**

测试用真实 `build_verify_report`：

```python
ACCIDENT = "专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。"
DOC02 = "第二章 学位授予条件。本科毕业生符合条件者授予学士学位。"
DOC06 = "第三十五条 专科起点本科的毕（结）业证书中注明“专\n科起点本科学习”。"


def test_accident_sentence_is_flagged():
    evidence = [Chunk(chunk_id="02:1", text=DOC02, document_id="02"),
                Chunk(chunk_id="02:2", text=DOC02, document_id="02")]
    report = build_verify_report(ACCIDENT, evidence)
    assert [c.sentence for c in report.unsupported_citations] == [ACCIDENT]
    assert {m.marker for m in report.unsupported_markers} == {1, 2}


def test_same_sentence_with_correct_document_is_not_flagged():
    evidence = [Chunk(chunk_id="06:12", text=DOC06, document_id="06")]
    report = build_verify_report(ACCIDENT, evidence)
    assert report.unsupported_citations == []
    assert report.unsupported_markers == []
```

- [ ] **Step 2: 历史落库端到端**

用 `test_conversations_api.py` 的模式：写一条带 `verify_report` 的助手消息 → `GET /messages` → 断言字段仍在。Task 5 已覆盖。

- [ ] **Step 3: 单/多轮评测端到端**

用 `test_eval_runner.py` 的 fake graph 和真实 DB：同一多轮用例分别 `dialogue_mode=single` 与 `multi` 跑，断言：
- single 只调用第一轮；
- multi 逐轮调用且 `metrics.turns` 长度 = 轮数；
- 报告 `dialogue_mode`、`multi_turn`、`ragas_scored_turns` 正确。

- [ ] **Step 4: 多轮生成端到端**

用 fake LLM + fake vector：`mode="multi"` 生成 1 条 ≥2 轮用例，断言每个 `ground_truth` 的实质分句都能在 `source_snippet` 里找到；`case_type='multi_turn'`、`question=turns[0].question`、`ground_truth IS NULL`。

- [ ] **Step 5: 跑全部相关测试**

```bash
cd D:\Knowledge_rag_system
uv run pytest \
  backend/tests/unit/test_citation_support.py \
  backend/tests/unit/test_cite.py \
  backend/tests/unit/test_prompts.py \
  backend/tests/unit/test_eval_report.py \
  backend/tests/unit/test_generate_multi_turn.py \
  backend/tests/integration/test_conversations_api.py \
  backend/tests/integration/test_eval_runner.py \
  backend/tests/integration/test_eval_api.py \
  backend/tests/integration/test_eval_compare.py \
  backend/tests/integration/test_eval_sets_crud.py \
  backend/tests/integration/test_eval_cases.py \
  backend/tests/integration/test_eval_generate.py \
  backend/tests/integration/test_eval_set_io.py \
  backend/tests/integration/test_eval_sets_migration.py \
  -q
```

Expected: 全绿。

- [ ] **Step 6: 前端构建与 probe**

```bash
cd frontend/web
npm run build
npx tsx scripts/annotation_probe/run.ts
```

Expected: 构建通过，probe 全绿。

- [ ] **Step 7: 迁移幂等验收**

```bash
cd backend
uv run python -m app.cli init-db
uv run python -m app.cli init-db
```

Expected: 两次成功；`eval_runs_dialogue_mode_check` 存在。

- [ ] **Step 8: 提交验收测试**

```bash
git add backend/tests/integration/test_citation_acceptance.py backend/tests/integration/test_eval_multiturn_acceptance.py
git commit -m "test: 引用与多轮评测端到端验收"
```

---

### Task 21: 最终回归与验收报告

**Files:**
- Create: `docs/superpowers/plans/2026-10-24-acceptance-report.md`

- [ ] **Step 1: 跑全量后端测试**

```bash
cd D:\Knowledge_rag_system
uv run pytest backend/tests -q
```

Expected: 全绿；若有与本次无关的历史失败，逐条记录在验收报告里，不掩盖。

- [ ] **Step 2: 跑前端构建**

```bash
cd frontend/web && npm run build
```

- [ ] **Step 3: 写验收报告**

报告必须逐条对应 spec §14.4 的 14 项功能验收，写清：
- 通过/未通过；
- 证据（测试名、命令输出摘要、截图/API 响应）；
- §14.1~14.3 的统计验收需要冻结标注集、人工复核和独立判官模型，本次**未执行**，说明原因与后续负责人；不能写成“已通过”。
- 已知差异：评测不做滚动压缩；无 `reference` 时上下文两项为空；支持度是字面规则。

- [ ] **Step 4: 提交**

```bash
git add docs/superpowers/plans/2026-10-24-acceptance-report.md
git commit -m "docs: 引用与多轮评测验收报告"
git push origin refactor/campus-rag
```

---

## Self-Review

**Spec coverage**
- §4 引用校验：Task 2/3/5/6 全覆盖；§4.6 测试逐条对应。
- §5 Prompt：Task 4。
- §6 单/多轮评测：Task 7/8/9/10/11。
- §7 多轮录入/生成：Task 12/13/14/15/16/17；§7.5 测试分布在各任务。
- §14.4 功能验收：Task 20/21；§14.1~14.3 统计验收在 Task 21 明确标为未执行，不伪造结果。

**Placeholder scan**
- 计划中没有 TBD/TODO/“类似 Task N”；每个任务给了文件、接口、测试或实现代码。
- 前端 UI 改动给了关键 JSX 与状态；样式改动给了完整 CSS。

**Type consistency**
- `UnsupportedClaim` / `UnsupportedMarker` 在 schema、citation_support、前端 types 三处字段一致：`marker/chunk_id/reason/score` 与 `sentence/char_start/char_end/markers/unsupported_markers/reason/score/cited_chunk_ids`。
- `dialogue_mode` 统一用 `Literal["single","multi"]`；DB 列同名。
- 多轮 turn 指标键统一为 `turns`、`turns_expected`、`turns_completed`、`turns_with_reference`；RAGAS 逐轮键为 `ragas_<metric>`。
- `sample_map` 统一为 `list[tuple[int, int]]`（case 结果下标、turn 下标）。
- `EvalCaseDetail` 继承 `EvalCaseItem`；前端 `getEvalCase` 类型来自生成的 `schema.d.ts`。
