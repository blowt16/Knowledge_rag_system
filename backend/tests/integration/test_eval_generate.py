"""从文档自动生成用例（§8）。

两道**硬校验**是这个功能的全部价值，所以下面主要盯它们：

① 标准答案必须是那段原文的**连续子串**（忽略空白）。
   模型改写、概括、跨段拼接都会当场露馅 —— 而只要漏一条，「标准答案」
   就不再是原文，评测跑出来的分数没有意义且看不出来。

② 问句里**不许有代词**。实测教训：单轮题带代词（「这个细则管的是哪些学生？」）
   会被图的 `resolve` 节点**正确**判成指代不明 → 走 `clarify`，
   于是评测里凭空多出「路由错」的假失败。代词是多轮题库在干的活儿。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app import db
from app.core import llm
from app.eval import generate, sets
from app.retrieval import vector

#: 造几段足够长的假 chunk（生成器会丢掉短于 120 字的片段 —— 造短了整批都会被滤掉，
#: 测试就变成「什么都没生成所以断言通过」的假绿）
_BODY = ("学生因病因事不能参加考试的，应当在考试前向所在学院提出缓考申请，"
         "经学院审核同意后报教务处备案，逾期不再受理。")
CHUNKS = [
    {"chunk_id": f"chunk-{i}", "text": f"第{i}条 {_BODY * 3}",
     "metadata": {"document_id": "doc-x", "chunk_index": i, "page": i + 1}}
    for i in range(6)
]
assert all(len(c["text"]) >= 120 for c in CHUNKS)


@pytest.fixture
async def scratch_set():
    set_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute("INSERT INTO eval_sets (id, name) VALUES ($1,$2)",
                           set_id, f"生成测试集-{set_id[:8]}")
    yield set_id
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


@pytest.fixture
async def fake_corpus(monkeypatch):
    """把向量库换成假的 —— 只验生成逻辑，不碰真索引。

    ⚠️ 文档 id 必须是**库里真实存在的**：`eval_cases.source_document_id` 有外键，
       编一个 uuid 会在入库那一刻 ForeignKeyViolation（这是对的，别改外键）。
       只把 `_load_document` 的**标题**换掉，id 用真的。
    """
    async with db.tx() as conn:
        doc_id = await conn.fetchval(
            "SELECT id FROM documents WHERE status='active' ORDER BY title LIMIT 1")
    if doc_id is None:
        pytest.skip("库里没有可用文档，跳过生成测试")
    monkeypatch.setattr(vector, "get_chunks", lambda *a, **kw: list(CHUNKS))
    monkeypatch.setattr(generate, "_load_document",
                        _stub_loader({"id": doc_id, "title": "某某管理办法"}))
    return doc_id


def _stub_loader(row):
    async def _load(conn, document_id):
        return row
    return _load


def _llm_returning(payload, delay: float = 0.0):
    async def fake(messages, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        return payload if not callable(payload) else payload(messages)
    return fake


# ============================================================
# 校验
# ============================================================

def test_squeeze_ignores_pdf_hard_wraps():
    assert generate.squeeze("学生 因病因事\n\n不能参加") == "学生因病因事不能参加"


def test_pronoun_check_catches_single_turn_killers():
    for bad in ["这个细则管的是哪些学生？", "该办法适用于谁？", "其适用范围是什么？"]:
        assert generate.has_pronoun(bad), bad
    for good in ["缓考申请要提前跟谁说？", "某某管理办法规定了什么？"]:
        assert not generate.has_pronoun(good), good


async def test_rewritten_answer_is_rejected(scratch_set, fake_corpus, monkeypatch):
    """模型用自己的话概括 —— 必须被拦下。"""
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        {"question": "缓考要跟谁说？", "ground_truth": "需要向学院申请并报教务处。"}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 1)
    assert stats["created"] == 0
    assert stats["failed"] == 1
    assert stats["reason"]


async def test_pronoun_question_is_rejected(scratch_set, fake_corpus, monkeypatch):
    """答案逐字来自原文，但问句带代词 —— 照样拦下。"""
    chunk = CHUNKS[0]["text"]
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        {"question": "这个规定说的是什么？", "ground_truth": chunk[:20]}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 1)
    assert stats["created"] == 0 and stats["failed"] == 1


async def test_verbatim_answer_passes_and_lands_in_the_db(scratch_set, fake_corpus,
                                                          monkeypatch):
    chunk = CHUNKS[0]["text"]
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        {"question": "缓考申请要交给谁？", "ground_truth": chunk[:20]}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 1)

    assert stats["created"] == 1 and stats["failed"] == 0
    assert stats["timeout"] is False
    async with db.tx() as conn:
        rows = await conn.fetch("SELECT * FROM eval_cases WHERE set_id = $1", scratch_set)
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["case_type"] == "factual"
    assert row["source"] == "generated"
    assert row["set_id"] == scratch_set
    assert row["expected_route"] == "knowledge"
    assert row["should_clarify"] == 0
    assert row["source_page"] == 1
    assert row["source_chunk_id"] == "chunk-0"
    assert row["source_snippet"] == chunk
    # ⚠️ 必须写 expected_doc_ids，否则这道题在轮次汇总里的 recall/mrr 恒为空（§8.1）
    assert json.loads(row["expected_doc_ids"]) == [fake_corpus]
    # 单轮、非受限题：这三个保持 NULL
    assert row["turns"] is None and row["visible_roles"] is None
    assert row["expected_chunk_ids"] is None


async def test_answer_must_come_from_its_own_chunk(scratch_set, fake_corpus, monkeypatch):
    """从**别的**片段抄来的答案也不行 —— 出题必须与本片段对得上。"""
    other = CHUNKS[3]["text"]
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        {"question": "缓考申请要交给谁？", "ground_truth": other[:25]}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 1)
    assert stats["created"] == 0


# ============================================================
# 上限 / 空库 / 超时
# ============================================================

async def test_count_is_clamped_to_ten(scratch_set, fake_corpus, monkeypatch):
    """生成条数上限 10（决策 18）。"""
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        lambda messages: {"question": "缓考申请要交给谁？",
                          "ground_truth": _chunk_of(messages)[:20]}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 99)
    assert stats["requested"] == generate.MAX_COUNT
    assert stats["created"] <= generate.MAX_COUNT


def _chunk_of(messages) -> str:
    """从 prompt 里把材料抠回来 —— 让假模型总能给出合法的逐字答案。"""
    text = messages[0]["content"]
    start = text.index('"""') + 3
    return text[start:text.index('"""', start)].strip()


async def test_document_without_chunks_reports_why(scratch_set, fake_corpus, monkeypatch):
    monkeypatch.setattr(vector, "get_chunks", lambda *a, **kw: [])
    stats = await generate.generate_cases(scratch_set, fake_corpus, 5)
    assert stats["created"] == 0
    assert "片段" in stats["reason"] or "索引" in stats["reason"]


async def test_short_chunks_are_ignored(scratch_set, fake_corpus, monkeypatch):
    """太短的片段出不了像样的题（旧脚本也是 >= 120 字才用）。"""
    monkeypatch.setattr(vector, "get_chunks",
                        lambda *a, **kw: [{"chunk_id": "c", "text": "短。",
                                           "metadata": {"page": 1}}])
    stats = await generate.generate_cases(scratch_set, fake_corpus, 5)
    assert stats["created"] == 0


async def test_timeout_keeps_what_was_already_generated(scratch_set, fake_corpus,
                                                        monkeypatch):
    """决策 18：总超时 120 秒，**超时返回部分结果**，已生成的留在库里。"""
    calls = {"n": 0}

    def payload(messages):
        calls["n"] += 1
        if calls["n"] > 1:
            return None          # 后续调用永远不返回 → 触发超时
        return {"question": "缓考申请要交给谁？",
                "ground_truth": _chunk_of(messages)[:20]}

    async def fake(messages, **kwargs):
        got = payload(messages)
        if got is None:
            await asyncio.sleep(30)
        return got

    monkeypatch.setattr(llm, "complete_json", fake)
    stats = await generate.generate_cases(scratch_set, fake_corpus, 3,
                                          timeout=1.5)

    assert stats["timeout"] is True
    assert stats["created"] >= 1, "超时前已经生成的必须留下"
    assert stats["created"] == len(stats["cases"])
    async with db.tx() as conn:
        kept = await conn.fetchval("SELECT count(*) FROM eval_cases WHERE set_id=$1",
                                   scratch_set)
    assert kept == stats["created"], "超时后库里的条数与响应不一致"


async def test_generated_cases_are_all_in_eval_and_visible_in_the_list(scratch_set,
                                                                      fake_corpus,
                                                                      monkeypatch):
    monkeypatch.setattr(llm, "complete_json", _llm_returning(
        lambda messages: {"question": "缓考申请要交给谁？",
                          "ground_truth": _chunk_of(messages)[:20]}))
    stats = await generate.generate_cases(scratch_set, fake_corpus, 3)
    async with db.tx() as conn:
        listed = await sets.list_cases(conn, scratch_set)
    assert listed["total"] == stats["created"]
    assert all(c["in_eval"] for c in listed["items"])
    assert all(c["source"] == "generated" for c in listed["items"])
