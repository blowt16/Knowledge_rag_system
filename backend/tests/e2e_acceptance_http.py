"""HTTP 端到端验收脚本（不属于 pytest 收集范围）。

跑法（仓库根）：`cd backend && uv run python tests/e2e_acceptance_http.py`

要求：
- PostgreSQL 已起；
- 新代码的后端跑在 127.0.0.1:8091；
- `.env` 里 LLM 可用（脚本会真实生成一条多轮用例）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx

from app import db
from app.core.security import hash_password
from app.graph.nodes.cite import build_verify_report
from app.graph.state import Chunk
from app.retrieval import vector

BASE = "http://127.0.0.1:8091"
PASSWORD = "E2e@12345"
ACCIDENT = "专科起点本科的毕（结）业证书中注明“专科起点本科学习”[1][2]。"
DOC02 = "ce65a6227fb241dbaddbce14acb070fd"
DOC06 = "7be0560db5f54384991cbc188e8acc7c"
REPO_ROOT = Path(__file__).resolve().parents[2]


def real_chunk(doc_id: str, needle: str) -> Chunk:
    for c in vector.get_chunks(doc_id, limit=500):
        if needle in (c.get("text") or ""):
            meta = c.get("metadata") or {}
            return Chunk(chunk_id=c["chunk_id"], text=c["text"],
                         document_id=doc_id,
                         document_name=meta.get("title", ""),
                         page=int(meta.get("page", 1)))
    raise RuntimeError(f"chunk not found: {needle}")


async def main() -> None:
    await db.init_pool()
    results: dict = {}
    admin_id = uuid.uuid4().hex
    student_id = uuid.uuid4().hex
    admin_name = f"e2e_admin_{admin_id[:6]}"
    student_name = f"e2e_student_{student_id[:6]}"
    conv_id = uuid.uuid4().hex
    set_id: str | None = None
    run_id: str | None = None
    try:
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO users (id,username,password_hash,role,token_version) "
                "VALUES ($1,$2,$3,'admin',0)",
                admin_id, admin_name, hash_password(PASSWORD))
            await conn.execute(
                "INSERT INTO users (id,username,password_hash,role,token_version) "
                "VALUES ($1,$2,$3,'student',0)",
                student_id, student_name, hash_password(PASSWORD))

        async with httpx.AsyncClient(base_url=BASE, timeout=300) as client:
            admin_login = await client.post(
                "/api/auth/login", json={"username": admin_name, "password": PASSWORD})
            student_login = await client.post(
                "/api/auth/login", json={"username": student_name, "password": PASSWORD})
            assert admin_login.status_code == 200, admin_login.text
            assert student_login.status_code == 200, student_login.text
            ah = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
            sh = {"Authorization": f"Bearer {student_login.json()['access_token']}"}

            # ---- 1) 事故复现 + 消息落库 → 刷新历史仍能读到 ----
            wrong = build_verify_report(
                ACCIDENT, [real_chunk(DOC02, "学位"), real_chunk(DOC02, "学位")])
            right = build_verify_report(ACCIDENT, [real_chunk(DOC06, "专科起点")])
            assert [x.sentence for x in wrong.unsupported_citations] == [ACCIDENT]
            assert right.unsupported_citations == [] and right.supported_claims == 1

            async with db.tx() as conn:
                await conn.execute(
                    "INSERT INTO conversations "
                    "(id,user_id,title,is_top,delete_flag,last_chat_time) "
                    "VALUES ($1,$2,'E2E引用',0,0,now())", conv_id, student_id)
                await conn.execute(
                    "INSERT INTO messages (id,conversation_id,role,content,route,created_at) "
                    "VALUES ($1,$2,'user','我是艺术类专业的学生需要达到什么条件',"
                    "'knowledge', now())", uuid.uuid4().hex, conv_id)
                await conn.execute(
                    "INSERT INTO messages "
                    "(id,conversation_id,role,content,citations,verify_report,created_at) "
                    "VALUES ($1,$2,'assistant',$3,$4::jsonb,$5::jsonb,"
                    " now() + interval '1 millisecond')",
                    uuid.uuid4().hex, conv_id, ACCIDENT, [], wrong.model_dump())

            r = await client.get(f"/api/conversations/{conv_id}/messages", headers=sh)
            assert r.status_code == 200, r.text
            report = r.json()["messages"][1]["verify_report"]
            assert report["unsupported_citations"][0]["sentence"] == ACCIDENT
            results["citation_history"] = {
                "wrong_sentence_flagged": True,
                "wrong_markers": [m["marker"] for m in report["unsupported_markers"]],
                "correct_document_not_flagged": True,
                "history_returns_verify_report": True,
            }

            # ---- 2) 多轮用例 CRUD / 详情 / 搜索 / 来源高亮 ----
            r = await client.post(
                "/api/admin/eval/sets",
                json={"name": f"E2E验收-{uuid.uuid4().hex[:8]}"}, headers=ah)
            assert r.status_code == 201, r.text
            set_id = r.json()["id"]
            r = await client.post(
                f"/api/admin/eval/sets/{set_id}/cases",
                json={"case_type": "multi_turn", "turns": [
                    {"question": "转专业需要什么条件？", "ground_truth": "第一条原文答案"},
                    {"question": "那材料呢？", "ground_truth": "第二条原文答案"},
                ]}, headers=ah)
            assert r.status_code == 201, r.text
            case_id = r.json()["id"]
            assert r.json()["turn_count"] == 2
            assert r.json()["question"] == "转专业需要什么条件？"

            r = await client.get(f"/api/admin/eval/cases/{case_id}", headers=ah)
            assert r.status_code == 200 and len(r.json()["turns"]) == 2, r.text

            r = await client.patch(
                f"/api/admin/eval/cases/{case_id}", headers=ah,
                json={"turns": [
                    {"question": "改后第一问", "ground_truth": "第一条原文答案"},
                    {"question": "那材料呢？", "ground_truth": "第二条原文答案"},
                ]})
            assert r.status_code == 200 and r.json()["question"] == "改后第一问", r.text

            r = await client.get(
                f"/api/admin/eval/sets/{set_id}/cases",
                params={"q": "材料呢"}, headers=ah)
            assert r.status_code == 200 and r.json()["total"] == 1, r.text

            snippet = "转专业需要什么条件。那材料呢。改后第一问。第一条原文答案。第二条原文答案。"
            async with db.tx() as conn:
                await conn.execute(
                    "UPDATE eval_cases SET source='generated', source_snippet=$2,"
                    " source_page=2, source_chunk_id='e2e:0' WHERE id=$1",
                    case_id, snippet)
            r = await client.get(f"/api/admin/eval/cases/{case_id}/source", headers=ah)
            assert r.status_code == 200 and len(r.json()["turn_highlights"]) == 2, r.text
            results["multi_case_crud"] = {
                "created": True,
                "question_derived": "改后第一问",
                "turn_count": 2,
                "search_later_turn": True,
                "detail_returns_turns": True,
                "source_turn_highlights": r.json()["turn_highlights"],
            }

            # ---- 3) 真实 LLM 多轮生成（HTTP）----
            # 单窗口可能被逐句原文校验拒绝（模型随机性）；用 3 个窗口，
            # 仍为 0 时再点一次 —— 与界面的「再点一次」语义一致。
            gen = None
            for _attempt in range(2):
                r = await client.post(
                    f"/api/admin/eval/sets/{set_id}/generate",
                    json={"document_id": DOC06, "count": 3, "mode": "multi",
                          "turns": 2, "window": 2,
                          "instruction": "围绕学生的申请条件与办理流程生成连续追问"},
                    headers=ah, timeout=300)
                assert r.status_code == 200, r.text
                gen = r.json()
                if gen["created"] >= 1:
                    break
            results["multi_generation_http"] = {
                "requested": gen["requested"],
                "created": gen["created"],
                "failed": gen["failed"],
                "timeout": gen["timeout"],
                "case_turn_counts": [c["turn_count"] for c in gen["cases"]],
                "reason": gen["reason"],
            }
            assert gen["created"] >= 1
            assert all(c["turn_count"] >= 2 for c in gen["cases"])

            # ---- 4) 多轮报告 / 对比页（API 透出）----
            run_id = uuid.uuid4().hex
            metrics = {
                "cases": 1, "failed": 0, "ragas_available": True, "ragas_errors": [],
                "turns_total": 2, "turns_completed": 2, "turns_with_reference": 2,
                "route_accuracy": 1.0, "clarify_accuracy": 1.0,
                "retrieval_hit_rate": 1.0, "resolve_anchor_hit_rate": 1.0,
                "citation_support_rate": 1.0, "unsupported_citations": 0,
                "unsupported_markers": 0, "ragas_scored_turns": 2,
                "ragas_scored_cases": 1,
                "multi_turn": {
                    "turns_total": 2, "turns_completed": 2, "turns_with_reference": 2,
                    "route_accuracy": 1.0, "clarify_accuracy": 1.0,
                    "retrieval_hit_rate": 1.0, "resolve_anchor_hit_rate": 1.0,
                    "citation_support_rate": 1.0, "unsupported_citations": 0,
                    "unsupported_markers": 0,
                },
                "ragas": {"faithfulness": 1.0, "answer_relevancy": 0.9,
                          "context_precision": None, "context_recall": None},
            }
            case_metrics = {"turns": [
                {"turn": 1, "question": "第一问", "route": "knowledge",
                 "route_ok": True, "ragas_faithfulness": 1.0,
                 "ragas_answer_relevancy": 0.9},
                {"turn": 2, "question": "第二问", "route": "knowledge",
                 "route_ok": True, "ragas_faithfulness": 1.0,
                 "ragas_answer_relevancy": 0.8},
            ]}
            async with db.tx() as conn:
                await conn.execute(
                    """INSERT INTO eval_runs
                         (id,name,config,role,include_restricted,status,set_id,set_name,
                          dialogue_mode,metrics,total_cases,done_cases,started_at,finished_at)
                       VALUES ($1,'E2E报告','{}'::jsonb,'student',0,'done',$2,$3,
                               'multi',$4::jsonb,1,1,now(),now())""",
                    run_id, set_id, f"E2E验收-{set_id[:8]}", metrics)
                await conn.execute(
                    """INSERT INTO eval_case_results
                         (id,run_id,case_id,retrieved_ids,answer,unauthorized_hits,
                          metrics,question,ground_truth,error)
                       VALUES ($1,$2,$3,$4::jsonb,'答',0,$5::jsonb,'问题',NULL,NULL)""",
                    uuid.uuid4().hex, run_id, case_id, [], case_metrics)

            r = await client.get(f"/api/admin/eval/runs/{run_id}", headers=ah)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["dialogue_mode"] == "multi"
            assert body["report"]["dialogue_mode"] == "multi"
            assert body["report"]["multi_turn"]["turns_total"] == 2
            assert body["report"]["ragas_scored_turns"] == 2
            assert len(body["cases"][0]["turns"]) == 2

            r = await client.get(
                "/api/admin/eval/compare", params={"run_ids": run_id}, headers=ah)
            assert r.status_code == 200, r.text
            cmp_body = r.json()
            assert cmp_body["runs"][0]["dialogue_mode"] == "multi"
            for key in ("citation_support_rate", "unsupported_markers",
                        "unsupported_citations", "ragas_scored_turns", "turns_total"):
                assert key in cmp_body["metrics"], key

            results["report_and_compare"] = {
                "dialogue_mode": body["dialogue_mode"],
                "multi_turn": body["report"]["multi_turn"],
                "ragas_scored_turns": body["report"]["ragas_scored_turns"],
                "case_turns": len(body["cases"][0]["turns"]),
                "compare_mode": cmp_body["runs"][0]["dialogue_mode"],
                "compare_has_new_metrics": True,
            }

        out = REPO_ROOT / "logs" / "e2e_http_acceptance.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        async with db.tx() as conn:
            if run_id:
                await conn.execute("DELETE FROM eval_runs WHERE id=$1", run_id)
            if set_id:
                await conn.execute("DELETE FROM eval_sets WHERE id=$1", set_id)
            await conn.execute("DELETE FROM messages WHERE conversation_id=$1", conv_id)
            await conn.execute("DELETE FROM conversations WHERE id=$1", conv_id)
            await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])",
                               [admin_id, student_id])
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
