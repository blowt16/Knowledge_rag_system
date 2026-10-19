"""端到端 bge vs jina 精排 A/B：20 题（10 表格 + 10 非表格）。

- 走真实图（route/rewrite/retrieve/rerank/build_context/generate/cite）；
- 两个后端同一批题、同一索引、同一 LLM 温度 0、`min_score=0`；
- 只切换 reranker.backend；
- 逐题记录：候选/精排/证据的期望文档命中、目标块命中、引用、答案、拒答、LLM 判正确。

⚠️ 运行前先停后端：Chroma/BM25 单写，且要和线上 Jina 抢显存。
⚠️ rerank 在主线程直接跑 `rerank_sync`，不走 asyncio.to_thread（Windows torch 段错误）。
"""
from __future__ import annotations

import asyncio
import gc
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from app import db
from app.core import llm as llm_mod
from app.eval import config as eval_config  # noqa: F401  (保留真实图的默认行为)
from app.graph.builder import get_graph
from app.graph.nodes.build_context import render_chunk_block
from app.graph.state import UserContextLite, new_state
from app.retrieval import reranker

EVAL_SETS = ROOT / "backend" / "eval_sets"
LOGS = ROOT / "logs"

# ---- 题目：10 非表格 + 10 表格 --------------------------------------
NON_TABLE_IDS = [
    "f-01-04", "f-02-01", "f-03-01", "f-04-01", "f-05-01",
    "f-06-01", "f-07-01", "f-08-01", "f-09-01", "f-10-01",
]
TABLE_IDS = [f"tq-{i:02d}" for i in range(1, 11)]

# 旧文档 id -> 当前重传后的文档 id（只有 doc11/doc12 变过）
DOC_ID_MAP = {
    "15d08f5db9a440bc8964d60514a81037": "3e8d190b3217413bad605714f1fe4692",  # doc12
    "8b524c9afff04143be747e66320eaf42": "e2be12c017584897b77df5aee24a979b",  # doc11
}

TABLE_TARGET_TOKENS = {
    "tq-01": ["参与公益活动", "无偿献血"],
    "tq-02": ["参与公益活动", "捐献造血干细胞"],
    "tq-03": ["大学英语及专业英语水平测试", "大学英语六级"],
    "tq-04": ["技能证书", "通过各级计算机等级考试并获得证书"],
    "tq-05": ["团支部书记、班长", "3分/学年"],
    "tq-06": ["校级学生组织负责人、团委兼职副书记", "10分/学年"],
    "tq-07": ["参加暑期“三下乡”社会实践（含专项）", "国家级立项"],
    "tq-08": ["参加校内勤工助学岗位", "2分/岗"],
    "tq-09": ["桂志愿", "1分/10小时"],
    "tq-10": ["专利类（第一作者单位为桂林电子科技大学）", "发明专利授权"],
}

# ---- 控制变量 --------------------------------------------------------
# LLM 温度固定为 0；只改 reranker 后端
_real_llm_cfg = llm_mod.cfg
llm_mod.cfg = lambda path, default=None: (
    0.0 if path == "llm.temperature" else _real_llm_cfg(path, default)
)

# rerank 节点原本 await rerank(...)（内部 asyncio.to_thread + torch）。
# 独立脚本在 Windows 上会段错误，这里改成主线程直跑 rerank_sync。
import app.graph.nodes.rerank as rerank_node_mod  # noqa: E402


async def _rerank_main(query, chunks, *, top_k=None):
    return reranker.rerank_sync(query, chunks, top_k=top_k)


rerank_node_mod.rerank = _rerank_main


def load_cases() -> list[dict]:
    default = json.loads(
        (EVAL_SETS / "默认题库.json").read_text(encoding="utf-8"))["cases"]
    table = json.loads(
        (EVAL_SETS / "二课表格口语题.json").read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in default + table}

    selected = []
    for cid in NON_TABLE_IDS:
        c = dict(by_id[cid])
        c["category"] = "non_table"
        selected.append(c)
    for cid in TABLE_IDS:
        c = dict(by_id[cid])
        c["category"] = "table"
        selected.append(c)
    return selected


def expected_ids(case: dict) -> set[str]:
    raw = case.get("expected_doc_ids") or []
    return {DOC_ID_MAP.get(x, x) for x in raw}


def switch_backend(name: str) -> None:
    if reranker._model is not None:
        reranker._model = None
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    reranker._load_failed = False
    reranker.backend = lambda _name=name: _name
    if reranker._load_model() is None:
        raise RuntimeError(f"{name} 模型加载失败")
    print(f"[backend] {name} loaded", flush=True)


async def judge_answer(question: str, ground_truth: str, answer: str) -> tuple[bool | None, str]:
    if not answer.strip():
        return False, "empty_answer"
    prompt = (
        "你是 RAG 答案评测员。判断【模型回答】是否包含【标准答案】的核心事实。\n"
        "标准答案可能是旧格式的键值串，只要语义一致就算正确；"
        "回答拒答、答非所问、关键数字/等级错误都算不正确。\n"
        "只输出 JSON：{\"correct\": true} 或 {\"correct\": false}\n\n"
        f"【问题】\n{question}\n\n"
        f"【标准答案】\n{ground_truth}\n\n"
        f"【模型回答】\n{answer[:3000]}"
    )
    try:
        raw = await llm_mod.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=200, json_mode=True,
        )
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        return bool(data.get("correct")), raw[:200]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _doc_hit(chunks: list, expected: set[str]) -> bool:
    return any(c.document_id in expected for c in chunks)


def _chunk_doc(chunk_id: str) -> str:
    return chunk_id.split(":", 1)[0]


async def run_case(backend: str, case: dict, graph) -> dict:
    sid = f"e2e-{backend}-{case['id']}"
    state = new_state(
        query=case["question"],
        session_id=sid,
        user=UserContextLite(id="eval", role="student"),
        history=[],
        include_restricted=False,
        eval_config={},
    )
    started = time.perf_counter()
    final = {}
    async for mode, payload in graph.astream(state, stream_mode=["values"]):
        if mode == "values":
            final = payload or {}
    elapsed = int((time.perf_counter() - started) * 1000)

    expected = expected_ids(case)
    candidates = final.get("candidates") or []
    reranked = final.get("reranked") or []
    evidence = final.get("evidence") or []
    citations = final.get("citations") or []
    answer = final.get("answer") or ""
    refused = bool(final.get("refused"))
    decision = final.get("decision") or ""

    target_hit = None
    if case["category"] == "table":
        tokens = TABLE_TARGET_TOKENS.get(case["id"], [])
        pool = evidence or reranked
        target_hit = any(all(tok in c.text for tok in tokens) for c in pool)

    citation_docs = {_chunk_doc(c.chunk_id) for c in citations}
    citation_hit = bool(citation_docs & expected)

    correct, judge_raw = (False, "refused") if refused else await judge_answer(
        case["question"], case.get("ground_truth") or "", answer)

    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "ground_truth": case.get("ground_truth"),
        "expected_doc_ids": sorted(expected),
        "route": final.get("route"),
        "decision": decision,
        "refused": refused,
        "refusal_reason": final.get("refusal_reason") or "",
        "answer": answer,
        "answer_preview": answer[:300],
        "correct": correct,
        "judge_raw": judge_raw,
        "candidate_count": len(candidates),
        "reranked_count": len(reranked),
        "evidence_count": len(evidence),
        "citation_count": len(citations),
        "candidate_doc_hit": _doc_hit(candidates, expected),
        "reranked_doc_hit": _doc_hit(reranked, expected),
        "evidence_doc_hit": _doc_hit(evidence, expected),
        "top1_doc_hit": bool(reranked) and reranked[0].document_id in expected,
        "target_hit": target_hit,
        "citation_hit": citation_hit,
        "retrieval_confidence": final.get("retrieval_confidence"),
        "rerank_degraded": bool(final.get("rerank_degraded")),
        "ms": elapsed,
        "reranked_ids": [c.chunk_id for c in reranked],
        "evidence_ids": [c.chunk_id for c in evidence],
        "context_preview": "\n".join(
            render_chunk_block(c, i) for i, c in enumerate(evidence[:2], start=1)
        )[:1500],
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {}
    def rate(key):
        return sum(1 for r in rows if r.get(key)) / n
    conf = [r["retrieval_confidence"] for r in rows
            if r.get("retrieval_confidence") is not None]
    return {
        "cases": n,
        "correct": sum(1 for r in rows if r["correct"] is True),
        "correct_rate": round(rate("correct"), 3),
        "refused": sum(1 for r in rows if r["refused"]),
        "candidate_doc_hit": sum(1 for r in rows if r["candidate_doc_hit"]),
        "reranked_doc_hit": sum(1 for r in rows if r["reranked_doc_hit"]),
        "evidence_doc_hit": sum(1 for r in rows if r["evidence_doc_hit"]),
        "top1_doc_hit": sum(1 for r in rows if r["top1_doc_hit"]),
        "target_hit": sum(1 for r in rows if r.get("target_hit") is True),
        "citation_hit": sum(1 for r in rows if r["citation_hit"]),
        "mean_confidence": round(statistics.mean(conf), 4) if conf else None,
        "mean_ms": round(statistics.mean(r["ms"] for r in rows), 1),
    }


def markdown(results: dict[str, list[dict]]) -> str:
    lines = ["# 端到端 bge vs jina 精排 A/B", "",
             "20 题：10 非表格 + 10 表格；同一索引、同一 LLM（temperature=0）、"
             "`min_score=0`；只切换 reranker。", ""]
    for backend, rows in results.items():
        lines += [f"## {backend}", "",
                  "| 类别 | 题数 | 正确 | 拒答 | 候选命中 | 精排命中 | 证据命中 | Top1 命中 | 目标块命中 | 引用命中 | 平均置信度 | 平均耗时(ms) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for cat in ("non_table", "table"):
            s = summarize([r for r in rows if r["category"] == cat])
            if not s:
                continue
            target = f"{s['target_hit']}/{s['cases']}" if cat == "table" else "-"
            lines.append(
                f"| {cat} | {s['cases']} | {s['correct']}/{s['cases']} | {s['refused']} | "
                f"{s['candidate_doc_hit']}/{s['cases']} | {s['reranked_doc_hit']}/{s['cases']} | "
                f"{s['evidence_doc_hit']}/{s['cases']} | {s['top1_doc_hit']}/{s['cases']} | "
                f"{target} | {s['citation_hit']}/{s['cases']} | "
                f"{s['mean_confidence']} | {s['mean_ms']} |")
        s = summarize(rows)
        lines.append(
            f"| **all** | {s['cases']} | {s['correct']}/{s['cases']} | {s['refused']} | "
            f"{s['candidate_doc_hit']}/{s['cases']} | {s['reranked_doc_hit']}/{s['cases']} | "
            f"{s['evidence_doc_hit']}/{s['cases']} | {s['top1_doc_hit']}/{s['cases']} | "
            f"{s['target_hit']}/{s['cases']} | {s['citation_hit']}/{s['cases']} | "
            f"{s['mean_confidence']} | {s['mean_ms']} |")
        lines.append("")

    lines += ["## 逐题", "",
              "| 题 | 类别 | bge 正确/拒答 | bge 证据命中/目标命中 | jina 正确/拒答 | jina 证据命中/目标命中 |",
              "|---|---|---|---|---|---|"]
    bge = {r["id"]: r for r in results.get("cross_encoder", [])}
    jina = {r["id"]: r for r in results.get("jina_listwise", [])}
    for cid in bge:
        b, j = bge[cid], jina.get(cid)
        if not j:
            continue
        def fmt(r):
            t = "yes" if r.get("target_hit") else ("no" if r.get("target_hit") is False else "-")
            return f"{'Y' if r['correct'] else 'N'} / {'Y' if r['refused'] else 'N'}", \
                   f"{'Y' if r['evidence_doc_hit'] else 'N'} / {t}"
        bc, be = fmt(b)
        jc, je = fmt(j)
        lines.append(
            f"| {cid} | {b['category']} | {bc} | {be} | {jc} | {je} |")
    return "\n".join(lines)


async def main() -> None:
    cases = load_cases()
    graph = get_graph()
    results: dict[str, list[dict]] = {}
    await db.init_pool()
    try:
        for backend in ("cross_encoder", "jina_listwise"):
            switch_backend(backend)
            results[backend] = []
            print(f"[{backend}] {len(cases)} cases", flush=True)
            for i, case in enumerate(cases, 1):
                row = await run_case(backend, case, graph)
                results[backend].append(row)
                print(f"  {i}/{len(cases)} {backend} {case['id']} "
                      f"correct={row['correct']} refused={row['refused']} "
                      f"ev={row['evidence_doc_hit']} target={row['target_hit']}",
                      flush=True)
    finally:
        await db.close_pool()

    payload = {"cases": cases, "results": results}
    out_json = LOGS / "e2e_rerank_ab.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    md = markdown(results)
    out_md = LOGS / "e2e_rerank_ab_summary.md"
    out_md.write_text(md, encoding="utf-8")
    print(md, flush=True)
    print(f"[saved] {out_json}")
    print(f"[saved] {out_md}")


if __name__ == "__main__":
    asyncio.run(main())
