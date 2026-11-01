"""多轮对话引用支持度实测（HTTP，真实 LLM）。

跑法（仓库根，后端需跑在 8090 且为**修复后**的代码）：
    cd backend && uv run python tools/multiturn_citation_probe.py

做的事：
- 建临时 admin/student 用户，登录；
- 每轮走真实 `/api/chat/stream`，多轮共用同一 session（模型能看到历史）；
- 从落库消息里取 `verify_report` / `citations` / 答案；
- 用被引 chunk 原文重算「旧规则」（只按 。！？ 切句）报告，跟新报告对照；
- 对每个被判不支持的编号，打印所在分句、缺失数字、召回率，便于人工判断真假阳性。

结束后删除临时用户、会话、消息、qa_logs、session_locks。结果写
`logs/multiturn_citation_probe.json`。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg
import httpx

from app.core.config import pg_dsn
from app.core.security import hash_password
from app.graph.nodes import citation_support as cs
from app.graph.nodes.cite import build_verify_report
from app.graph.state import Chunk

BASE = "http://127.0.0.1:8090"
PASSWORD = "Mt@12345"
OUT = ROOT / "logs" / "multiturn_citation_probe.json"

# 多轮用例：每组的第 2 轮起都是追问（历史残留、编号重排、长句并列引用的高发区）
SESSIONS: list[tuple[str, list[str]]] = [
    ("学位授予与艺术类", [
        "学士学位授予条件是什么？",
        "我是艺术类专业的学生需要达到什么条件？",
        "那外语成绩具体要求多少分？",
        "专科起点本科的毕业证书上会怎么注明？",
    ]),
    ("毕业与结业", [
        "本科生毕业需要满足哪些条件？",
        "结业和肄业有什么区别？",
        "专科起点本科的学位证书怎么发？",
    ]),
    ("转专业与退役复学", [
        "转专业的申请条件是什么？",
        "那需要提交哪些材料？",
        "办理流程要多久？",
        "退役复学的学生有什么优待政策？",
    ]),
]

# 修复前的切句规则：只认 。！？
OLD_SENTENCE_RE = re.compile(r"[^。！？\n]*[。！？\n]?")
OLD_MARKER_RE = re.compile(r"\[(\d+)\]")


def _old_supports(sentence: str, chunk_text: str,
                  cited_numbers: set[str] | None = None):
    """修复前的数字规则：所有方括号数字都当角标、数字按单个片段校验。"""
    plain = OLD_MARKER_RE.sub("", sentence)
    units = cs.content_units(plain)
    if not units:
        return False, 0.0, "no_overlap"
    chunk_units = cs.content_units(chunk_text)
    recall = len(units & chunk_units) / len(units)
    numbers = cs._NUMBER_RE.findall(cs.normalize(plain))
    chunk_numbers = set(cs._NUMBER_RE.findall(cs.normalize(chunk_text)))
    number_ok = all(n in chunk_numbers for n in numbers)
    long_common = cs._has_long_common(plain, chunk_text)
    supported = number_ok and (recall >= cs.SUPPORT_UNIT_RECALL or long_common)
    if not number_ok:
        reason = "number_mismatch"
    elif recall < cs.SUPPORT_UNIT_RECALL and not long_common:
        reason = "weak_overlap" if numbers else "no_overlap"
    else:
        reason = "no_overlap"
    return supported, round(recall, 4), reason
# 修复后的切句规则（含分号）
NEW_SENTENCE_RE = re.compile(r"[^。！？\n；;]*[。！？\n；;]?")


async def _login(client: httpx.AsyncClient, username: str) -> str:
    r = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


async def _chat_turn(client: httpx.AsyncClient, token: str, query: str,
                     session_id: str | None) -> tuple[str, dict]:
    """跑一轮，返回 (session_id, 事件字典)。"""
    payload = {"query": query, "request_id": uuid.uuid4().hex}
    if session_id:
        payload["session_id"] = session_id
    events: dict[str, dict] = {}
    event: str | None = None
    data: list[str] = []
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with client.stream("POST", "/api/chat/stream", headers={"Authorization": f"Bearer {token}"},
                             json=payload, timeout=timeout) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "":
                if event and data:
                    events[event] = json.loads("\n".join(data))
                event, data = None, []
    if "error" in events:
        raise RuntimeError(f"chat error: {events['error']}")
    sid = (events.get("session_created") or {}).get("session_id") or session_id
    return sid, events


async def _last_assistant(client: httpx.AsyncClient, token: str, session_id: str) -> dict:
    r = await client.get(f"/api/conversations/{session_id}/messages",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    msgs = [m for m in r.json()["messages"] if m.get("role") == "assistant"]
    if not msgs:
        raise RuntimeError("落库消息里没有 assistant")
    return msgs[-1]


async def _fetch_chunks(client: httpx.AsyncClient, token: str, doc_id: str) -> dict[int, str]:
    """按 chunk_index 取全文（管理端预览接口，不需要另开 Chroma 进程）。"""
    out: dict[int, str] = {}
    page = 1
    while True:
        r = await client.get(f"/api/admin/documents/{doc_id}/chunks",
                             headers={"Authorization": f"Bearer {token}"},
                             params={"page": page, "page_size": 100})
        r.raise_for_status()
        body = r.json()
        for c in body["chunks"]:
            out[int(c["chunk_index"])] = c.get("text") or ""
        if not body.get("has_more"):
            return out
        page += 1


def _evidence_from_citations(citations: list[dict],
                             chunk_texts: dict[tuple[str, int], str]) -> list[Chunk]:
    by_marker = {int(c["marker"]): c for c in citations}
    if not by_marker:
        return []
    evidence: list[Chunk] = []
    for marker in range(1, max(by_marker) + 1):
        c = by_marker.get(marker)
        if not c:
            evidence.append(Chunk(chunk_id=f"missing:{marker}", text=""))
            continue
        doc_id = (c.get("jump_target") or {}).get("document_id", "")
        idx = int(str(c["chunk_id"]).rsplit(":", 1)[-1])
        evidence.append(Chunk(chunk_id=c["chunk_id"],
                              text=chunk_texts.get((doc_id, idx), "")))
    return evidence


def _old_report(answer: str, evidence: list[Chunk]):
    """修复前的三条规则：只按 。！？ 切句、所有方括号数字都当角标、数字按单段校验。"""
    saved = (cs._SENTENCE_RE, cs._MARKER_RE, cs._supports)
    try:
        cs._SENTENCE_RE = OLD_SENTENCE_RE
        cs._MARKER_RE = OLD_MARKER_RE
        cs._supports = _old_supports
        return build_verify_report(answer, evidence)
    finally:
        cs._SENTENCE_RE, cs._MARKER_RE, cs._supports = saved


def _marker_clauses(answer: str, marker: int, regex: re.Pattern[str]) -> list[str]:
    needle = f"[{marker}]"
    return [m.group(0).strip() for m in regex.finditer(answer) if needle in m.group(0)]


def _excerpt(text: str, clause: str, width: int = 90) -> str:
    """取 chunk 里与分句最长公共 4-gram 附近的一段，便于人工核对。"""
    grams = cs._cjk_ngrams(clause)
    if not grams:
        return ""
    hits = [g for g in grams if g in text]
    if not hits:
        return ""
    pos = text.find(hits[0])
    # 用离命中位置最近的 4-gram 代替“最长”
    for g in hits:
        p = text.find(g)
        if p >= 0:
            pos = p
            break
    lo, hi = max(0, pos - width), min(len(text), pos + width)
    return text[lo:hi].replace("\n", " ")


def _marker_detail(answer: str, marker: int, chunk: str, regex: re.Pattern[str],
                   chunks_by_marker: dict[int, str]) -> dict:
    clause_list = _marker_clauses(answer, marker, regex)
    details = []
    for clause in clause_list:
        markers_in_clause = [int(m) for m in cs._MARKER_RE.findall(clause)]
        cited_numbers = {
            n for mk in markers_in_clause
            for n in cs._NUMBER_RE.findall(cs.normalize(chunks_by_marker.get(mk, "")))
        }
        ok, score, reason = cs._supports(clause, chunk, cited_numbers)
        details.append({
            "clause": clause,
            "missing_numbers": [n for n in cs._gate_numbers(clause)
                                if n not in cited_numbers],
            "supported": ok,
            "recall": score,
            "reason": reason,
            "chunk_excerpt": _excerpt(chunk, clause),
        })
    return {"marker": marker, "clauses": details}


async def main() -> None:
    conn = await asyncpg.connect(pg_dsn())
    admin_id, student_id = uuid.uuid4().hex, uuid.uuid4().hex
    admin_name = f"mt_admin_{admin_id[:6]}"
    student_name = f"mt_student_{student_id[:6]}"
    session_ids: list[str] = []
    results: list[dict] = []
    only = ({int(x) for x in sys.argv[1].split(",")} if len(sys.argv) > 1 else None)
    try:
        async with conn.transaction():
            for uid, name, role in ((admin_id, admin_name, "admin"),
                                    (student_id, student_name, "student")):
                await conn.execute(
                    "INSERT INTO users (id,username,password_hash,role,token_version) "
                    "VALUES ($1,$2,$3,$4,0)", uid, name, hash_password(PASSWORD), role)

        async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
            student_token = await _login(client, student_name)
            admin_token = await _login(client, admin_name)
            sh = {"Authorization": f"Bearer {student_token}"}
            ah = {"Authorization": f"Bearer {admin_token}"}

            chunk_cache: dict[tuple[str, int], str] = {}

            for session_idx, (session_name, questions) in enumerate(SESSIONS, start=1):
                if only and session_idx not in only:
                    continue
                sid: str | None = None
                print(f"\n########## 会话：{session_name} ##########", flush=True)
                for i, question in enumerate(questions, start=1):
                    sid, events = await _chat_turn(client, student_token, question, sid)
                    if sid and sid not in session_ids:
                        session_ids.append(sid)
                    msg = await _last_assistant(client, student_token, sid)
                    answer, citations = msg["content"], msg.get("citations") or []
                    report = msg.get("verify_report") or {}
                    route = (events.get("route") or {}).get("route") or msg.get("route") or ""

                    # 取被引 chunk 原文（缓存）
                    for c in citations:
                        doc_id = (c.get("jump_target") or {}).get("document_id", "")
                        if doc_id and not any(k[0] == doc_id for k in chunk_cache):
                            fetched = await _fetch_chunks(client, admin_token, doc_id)
                            for idx, text in fetched.items():
                                chunk_cache[(doc_id, idx)] = text
                    evidence = _evidence_from_citations(citations, chunk_cache)
                    old = _old_report(answer, evidence)

                    chunks_by_marker = {i + 1: c.text for i, c in enumerate(evidence)}
                    old_details = [
                        _marker_detail(answer, m.marker, evidence[m.marker - 1].text,
                                       NEW_SENTENCE_RE, chunks_by_marker)
                        for m in old.unsupported_markers
                        if 1 <= m.marker <= len(evidence)
                    ]
                    new_details = [
                        _marker_detail(answer, m["marker"], evidence[m["marker"] - 1].text,
                                       NEW_SENTENCE_RE, chunks_by_marker)
                        for m in report.get("unsupported_markers", [])
                        if 1 <= m["marker"] <= len(evidence)
                    ]

                    item = {
                        "session": session_name,
                        "turn": i,
                        "question": question,
                        "route": route,
                        "decision": (events.get("decision") or {}).get("decision"),
                        "answer": answer,
                        "citations": [
                            {"marker": c["marker"], "chunk_id": c["chunk_id"],
                             "document_name": c.get("document_name")}
                            for c in citations
                        ],
                        "new": {
                            "supported_claims": report.get("supported_claims"),
                            "unsupported_citations": len(report.get("unsupported_citations") or []),
                            "unsupported_markers": [
                                {"marker": m["marker"], "reason": m["reason"],
                                 "score": m["score"], "chunk_id": m["chunk_id"]}
                                for m in report.get("unsupported_markers", [])
                            ],
                            "details": new_details,
                        },
                        "old": {
                            "supported_claims": old.supported_claims,
                            "unsupported_citations": len(old.unsupported_citations),
                            "unsupported_markers": [
                                {"marker": m.marker, "reason": m.reason,
                                 "score": m.score, "chunk_id": m.chunk_id}
                                for m in old.unsupported_markers
                            ],
                            "details": old_details,
                        },
                    }
                    results.append(item)

                    n_old = len(old.unsupported_markers)
                    n_new = len(report.get("unsupported_markers") or [])
                    print(f"\n--- 第 {i} 轮 [{route}] {question}")
                    print(f"    旧规则标记 {n_old} 个 / 新规则标记 {n_new} 个")
                    for m in old.unsupported_markers:
                        print(f"      OLD [{m.marker}] {m.reason} score={m.score}")
                    for d in old_details:
                        for c in d["clauses"]:
                            if not c["supported"]:
                                print(f"        分句: {c['clause'][:110]}")
                                print(f"        缺数字: {c['missing_numbers']} recall={c['recall']}")
                    for m in report.get("unsupported_markers", []):
                        print(f"      NEW [{m['marker']}] {m['reason']} score={m['score']}")
                    if not n_old and not n_new:
                        print("    （无标记）")

        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写：{OUT}")
    finally:
        async with conn.transaction():
            qa_ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM qa_logs WHERE session_id = ANY($1::text[])", session_ids)]
            if qa_ids:
                await conn.execute("DELETE FROM degradation_events WHERE qa_log_id = ANY($1::text[])", qa_ids)
                await conn.execute("DELETE FROM refusal_annotations WHERE qa_log_id = ANY($1::text[])", qa_ids)
                await conn.execute("DELETE FROM qa_logs WHERE id = ANY($1::text[])", qa_ids)
            await conn.execute("DELETE FROM session_locks WHERE session_id = ANY($1::text[])", session_ids)
            await conn.execute(
                "DELETE FROM messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE user_id = ANY($1::text[]))",
                [admin_id, student_id])
            await conn.execute("DELETE FROM conversations WHERE user_id = ANY($1::text[])",
                               [admin_id, student_id])
            await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])",
                               [admin_id, student_id])
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
