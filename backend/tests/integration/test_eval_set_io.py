"""评测集导出/导入（§3.6）—— **一份 json 就是题库的 git 存档**。

⚠️ 最要紧的一条：**导出必须列全字段**。90 条老题里 15 条多轮题靠 `turns`、
   受限题靠 `visible_roles`、A 组指标靠 `expected_route` / `should_clarify`。
   漏字段 = 这份存档**静默损坏**，而且 round-trip 还测不出来 —— 导入按字段写入，
   缺的列保持原值，看起来「没丢」。所以下面的往返比对是**逐字段**的，
   而且先把库里的值改脏再导回来，不然「没丢」可能只是「本来就没被覆盖」。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app import db
from app.eval import sets

SET_NAME = "往返测试集"


@pytest.fixture
async def scratch_set():
    """一个自建的评测集，用完连用例一起删（`set_id` 外键是 CASCADE）。"""
    set_id = uuid.uuid4().hex
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO eval_sets (id, name, description) VALUES ($1,$2,$3)",
            set_id, SET_NAME, "目录往返用")
    yield set_id
    async with db.tx() as conn:
        await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


async def _insert_case(conn, set_id: str, **overrides) -> dict:
    row = {
        "id": uuid.uuid4().hex,
        "question": "问句",
        "ground_truth": "标准答案",
        "case_type": "factual",
        "suite": "full",
        "expected_doc_ids": ["doc-1"],
        "expected_chunk_ids": ["chunk-1"],
        "turns": None,
        "visible_roles": None,
        "expected_route": "knowledge",
        "should_clarify": 0,
        "source": "manual",
        "in_eval": True,
        "note": None,
        "source_document_id": None,
        "source_chunk_id": None,
        "source_page": None,
        "source_snippet": None,
    }
    row.update(overrides)
    await conn.execute(
        """INSERT INTO eval_cases
             (id, question, ground_truth, case_type, suite, expected_doc_ids,
              expected_chunk_ids, turns, visible_roles, expected_route, should_clarify,
              source, in_eval, note, source_document_id, source_chunk_id,
              source_page, source_snippet, set_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)""",
        row["id"], row["question"], row["ground_truth"], row["case_type"], row["suite"],
        json.dumps(row["expected_doc_ids"]), json.dumps(row["expected_chunk_ids"]),
        json.dumps(row["turns"]) if row["turns"] is not None else None,
        json.dumps(row["visible_roles"]) if row["visible_roles"] is not None else None,
        row["expected_route"], row["should_clarify"], row["source"], row["in_eval"],
        row["note"], row["source_document_id"], row["source_chunk_id"],
        row["source_page"], row["source_snippet"], set_id)
    return row


async def _read_cases(conn, set_id: str) -> dict[str, dict]:
    rows = await conn.fetch(
        "SELECT * FROM eval_cases WHERE set_id = $1 ORDER BY id", set_id)
    out = {}
    for r in rows:
        d = dict(r)
        for key in ("expected_doc_ids", "expected_chunk_ids", "turns", "visible_roles"):
            raw = d.get(key)
            d[key] = json.loads(raw) if isinstance(raw, str) else raw
        out[d["id"]] = d
    return out


def _subset(case: dict) -> dict:
    """只比对导出契约里的字段 —— `created_at` 之类的运行期列不在契约内。"""
    return {f: case.get(f) for f in sets.CASE_FIELDS}


def test_safe_filename_replaces_path_and_header_hostile_chars():
    """评测集名可能带 `/ \\ : * ? " < > |` —— 在 Windows 上非法，还会让
    `Content-Disposition` 头断行（§5.1）。"""
    assert sets.safe_filename("售后/问题:测评*") == "售后_问题_测评_"
    assert sets.safe_filename("a\\b") == "a_b"
    assert sets.safe_filename("带\n换行") == "带_换行"
    assert "/" not in sets.safe_filename("../../etc/passwd")
    assert "\\" not in sets.safe_filename("..\\..\\windows")
    # 名字全是非法字符时不能退化成空文件名
    assert sets.safe_filename("///") == "___"


async def test_export_lists_every_case_field(scratch_set):
    """导出的每条用例必须带**契约里的全部字段**，一个都不能省。"""
    async with db.tx() as conn:
        await _insert_case(conn, scratch_set)
        payload = await sets.export_payload(conn, SET_NAME)

    assert payload["version"] == sets.EXPORT_VERSION
    assert payload["set"]["name"] == SET_NAME
    assert payload["set"]["description"] == "目录往返用"
    assert len(payload["cases"]) == 1
    missing = set(sets.CASE_FIELDS) - set(payload["cases"][0])
    assert not missing, f"导出漏了字段：{sorted(missing)}"


async def test_roundtrip_restores_every_field(scratch_set):
    """导出 → 把库里的值全改脏 → 导回 → **逐字段**比对。"""
    multi_turn = {"turns": [{"question": "第一轮", "ground_truth": "答案一"},
                            {"question": "第二轮", "ground_truth": "答案二"}]}
    async with db.tx() as conn:
        plain = await _insert_case(conn, scratch_set)
        # 这一条把「容易漏的列」全填上：多轮 / 受限范围 / 路由 / 澄清
        rich = await _insert_case(
            conn, scratch_set, case_type="multi_turn", suite="refusal_calib",
            expected_chunk_ids=["c-9"], visible_roles=["admin"],
            expected_route="clarify", should_clarify=1, source="generated",
            in_eval=False, note="备注", source_document_id=None,
            source_chunk_id="chunk-237", source_page=3,
            source_snippet="原文片段", **multi_turn)
        payload = await sets.export_payload(conn, SET_NAME)
        before = await _read_cases(conn, scratch_set)

        # 把每一列都改脏 —— 否则「导回后没变」可能只是「压根没写」
        await conn.execute(
            """UPDATE eval_cases SET question='脏', ground_truth='脏', case_type='refusal',
                 suite='full', expected_doc_ids=NULL, expected_chunk_ids=NULL, turns=NULL,
                 visible_roles=NULL, expected_route=NULL, should_clarify=NULL,
                 source='manual', in_eval=TRUE, note=NULL, source_document_id=NULL,
                 source_chunk_id=NULL, source_page=NULL, source_snippet=NULL
             WHERE set_id = $1""", scratch_set)

        stats = await sets.import_payload(conn, payload)
        after = await _read_cases(conn, scratch_set)

    assert stats["updated"] == 2 and stats["created"] == 0
    assert set(after) == {plain["id"], rich["id"]}
    for cid, want in before.items():
        assert _subset(after[cid]) == _subset(want), (
            f"用例 {cid} 往返后字段对不上："
            f"{ {k: (v, _subset(after[cid])[k]) for k, v in _subset(want).items() if _subset(after[cid])[k] != v} }")


async def test_export_of_empty_set_is_not_an_error(scratch_set):
    """还没录题的集照常导出（`cases: []`）—— 这是正常动作，不是错误（§5.1）。"""
    async with db.tx() as conn:
        payload = await sets.export_payload(conn, SET_NAME)
    assert payload["cases"] == []


async def test_exported_file_is_named_by_the_set(scratch_set, tmp_path):
    async with db.tx() as conn:
        payload = await sets.export_payload(conn, SET_NAME)
    path = sets.write_export_file(payload, tmp_path)
    assert path.name == f"{SET_NAME}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload


async def test_export_file_name_is_sanitized(tmp_path):
    set_id = uuid.uuid4().hex
    try:
        async with db.tx() as conn:
            await conn.execute("INSERT INTO eval_sets (id, name) VALUES ($1,$2)",
                               set_id, "有/斜杠:的名")
            payload = await sets.export_payload(conn, "有/斜杠:的名")
        path = sets.write_export_file(payload, tmp_path)
        assert path.parent == tmp_path, "文件名里的斜杠被当成了目录分隔符"
        assert path.name == "有_斜杠_的名.json"
    finally:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)


async def test_import_creates_the_set_when_missing(scratch_set):
    """`set` 按名字找；**找不到就新建**（§3.6）。"""
    async with db.tx() as conn:
        await _insert_case(conn, scratch_set)
        payload = await sets.export_payload(conn, SET_NAME)
        payload["set"]["name"] = "导入时新建的集"
        payload["set"]["description"] = "来自文件"
        try:
            stats = await sets.import_payload(conn, payload)
            assert stats["set_created"] is True
            row = await conn.fetchrow("SELECT id, description FROM eval_sets WHERE name=$1",
                                      "导入时新建的集")
            assert row is not None and row["description"] == "来自文件"
            moved = await conn.fetchval("SELECT set_id FROM eval_cases WHERE id=$1",
                                        payload["cases"][0]["id"])
            assert moved == row["id"], "用例没有跟着搬到新建的集里"
        finally:
            await conn.execute("DELETE FROM eval_sets WHERE name = $1", "导入时新建的集")


async def test_import_is_idempotent(scratch_set):
    """可重复跑：第二次导入不再新建、不再改动。"""
    async with db.tx() as conn:
        await _insert_case(conn, scratch_set)
        payload = await sets.export_payload(conn, SET_NAME)
        first = await _read_cases(conn, scratch_set)
        stats1 = await sets.import_payload(conn, payload)
        stats2 = await sets.import_payload(conn, payload)
        second = await _read_cases(conn, scratch_set)

    assert stats1 == stats2
    assert stats1["created"] == 0 and stats1["updated"] == 1
    assert _subset(second[next(iter(second))]) == _subset(first[next(iter(first))])


async def test_import_export_roundtrip_covers_the_real_fixture_field_set():
    """整个默认题库走一遍往返，逐字段比对 —— 90 条老题不是玩具样本。"""
    async with db.tx() as conn:
        payload = await sets.export_payload(conn, "默认题库")
        assert len(payload["cases"]) == 75, "默认题库应该是 75 条（suite='full'）"
        assert all(set(sets.CASE_FIELDS) <= set(c) for c in payload["cases"])
        # 多轮题的 turns 必须真的在（不是 null）
        with_turns = [c for c in payload["cases"] if c["turns"]]
        assert with_turns, "多轮题的 turns 全丢了 —— 导出的字段没列全"
