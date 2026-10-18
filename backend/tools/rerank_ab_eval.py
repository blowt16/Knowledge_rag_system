"""两轮 bge vs jina 精排对比（施工后评测）。

候选池：用**当前代码**重新生成 doc 11/12 的全部 chunk（不依赖线上索引）。
每轮：同一批候选、同一批问题，分别用 bge / jina 精排，记录目标块分数与排名。
⚠️ 在主线程直接跑 `_predict`，不走 asyncio/to_thread —— Windows 上会撞
   torch/CUDA 线程池退出段错误（见项目记忆）。
"""
from __future__ import annotations

import gc
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.graph.state import Chunk
from app.ingestion.chunker import chunk_text
from app.ingestion.clean import build_normalized_text, page_of_offset
from app.ingestion.loaders.pdf import load_pdf
from app.ingestion.tables import table_atomic_ranges
from app.retrieval import reranker

CORPUS = ROOT / "corpus" / "guet"
EVAL_SETS = ROOT / "backend" / "eval_sets"
LOGS = ROOT / "logs"

DOCS = {
    "doc11": "11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf",
    "doc12": "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf",
}

# case_id -> 目标块里唯一会出现的一段文本
TARGETS = {
    "tq-01": "参与公益活动 - 无偿献血，参考学分 2分/次",
    "tq-02": "捐献造血干细胞，参考学分 10分/次",
    "tq-03": "大学英语及专业英语水平测试：大学英语六级，参考学分 5分/项",
    "tq-04": "计算机等级证书：通过各级计算机等级考试并获得证书，参考学分 3分/项",
    "tq-05": "团支部书记、班长，参考学分 3分/学年",
    "tq-06": "校级学生组织负责人、团委兼职副书记，参考学分 10分/学年",
    "tq-07": "国家级立项，参考学分 15分/项",
    "tq-08": "勤工助学 - 参加校内勤工助学岗位，参考学分 2分/岗",
    "tq-09": "每学年“桂志愿”累计志愿服务时长",
    "tq-10": "发明专利授权，参考学分 20分/项",
    "tq-11": "软件著作权，参考学分 10分/项",
    "tq-12": "文体竞赛：国家级一等奖18分/项",
    "tq-13": "党校培训：完成党校培训课时并结业",
    "tq-14": "新生入学教育：完成相应培训课时，参考学分 2分/期",
    "tq-15": "国家级特等奖（金奖）30分/项",
    "tq-16": "创业实体入驻校创新创业基地或孵化中心，参考学分 15分/个",
    "tq-17": "企业实训 - 围绕企业实战项目开展为期半年以上的实习实践并考核合格",
    "sc-01": "参与公益活动 - 无偿献血，参考学分 2分/次",
    "sc-02": "捐献造血干细胞，参考学分 10分/次",
    "sc-03": "大学英语及专业英语水平测试：大学英语六级，参考学分 5分/项",
    "sc-04": "计算机等级证书：通过各级计算机等级考试并获得证书，参考学分 3分/项",
    "sc-05": "校级学生组织负责人、团委兼职副书记，参考学分 10分/学年",
    "sc-06": "文体竞赛：国家级一等奖18分/项",
    "sc-07": "国家级特等奖（金奖）30分/项",
    "sc-08": "国家级立项，参考学分 15分/项",
    "sc-09": "团支部书记、班长，参考学分 3分/学年",
    "sc-10": "思想政治与道德修养”类学分不少于15",
}


def build_chunks() -> list[Chunk]:
    out: list[Chunk] = []
    for doc_key, filename in DOCS.items():
        result = load_pdf(CORPUS / filename)
        normalized, slices = build_normalized_text(
            [(p.page, p.text) for p in result.pages])
        spans = chunk_text(
            normalized,
            boundaries=[s.char_start for s in slices[1:]],
            atomic=table_atomic_ranges(normalized),
        )
        for span in spans:
            out.append(Chunk(
                chunk_id=f"{doc_key}:{span.chunk_index}",
                chunk_index=span.chunk_index,
                document_id=doc_key,
                document_name=doc_key,
                text=span.text,
                char_start=span.char_start,
                char_end=span.char_end,
                page=page_of_offset(slices, span.char_start),
            ))
    return out


def load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"]


def target_chunk(case_id: str, chunks: list[Chunk]) -> Chunk:
    phrase = TARGETS[case_id]
    hits = [c for c in chunks if phrase in c.text]
    if len(hits) != 1:
        raise RuntimeError(f"{case_id} 目标短语命中 {len(hits)} 个 chunk：{phrase!r}")
    return hits[0]


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
    model = reranker._load_model()
    if model is None:
        raise RuntimeError(f"{name} 模型加载失败")
    print(f"[backend] {name} loaded", flush=True)


def run_round(name: str, cases: list[dict], chunks: list[Chunk],
              backend: str) -> list[dict]:
    print(f"[round {name}] {backend}: {len(cases)} cases x {len(chunks)} chunks",
          flush=True)
    rows: list[dict] = []
    for n, case in enumerate(cases, 1):
        question = case["question"]
        target = target_chunk(case["id"], chunks)
        scores = reranker._predict(question, chunks)
        ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
        rank = next(i + 1 for i, (c, _s) in enumerate(ranked)
                    if c.chunk_id == target.chunk_id)
        target_score = next(s for c, s in ranked if c.chunk_id == target.chunk_id)
        top5 = [
            {"chunk_id": c.chunk_id, "score": round(float(s), 6),
             "text": c.text[:120]}
            for c, s in ranked[:5]
        ]
        rows.append({
            "id": case["id"],
            "question": question,
            "target_chunk_id": target.chunk_id,
            "target_page": target.page,
            "target_score": round(float(target_score), 6),
            "target_rank": rank,
            "candidate_count": len(chunks),
            "top5": top5,
        })
        print(f"  {n}/{len(cases)} {case['id']} rank={rank} "
              f"score={target_score:.4f}", flush=True)
    return rows


def summarize(rows: list[dict], backend: str) -> dict:
    scores = [r["target_score"] for r in rows]
    ranks = [r["target_rank"] for r in rows]
    # 阈值只用于展示；是否过线按当前后端自己的 min_score 另算
    min_score = float(reranker._pcfg("min_score", 0.3))
    return {
        "backend": backend,
        "min_score": min_score,
        "cases": len(rows),
        "mean_target_score": round(statistics.mean(scores), 6),
        "median_target_score": round(statistics.median(scores), 6),
        "mean_target_rank": round(statistics.mean(ranks), 3),
        "top1": sum(1 for r in rows if r["target_rank"] == 1),
        "top5": sum(1 for r in rows if r["target_rank"] <= 5),
        "above_threshold": sum(1 for r in rows
                               if r["target_score"] >= min_score),
    }


def markdown(round_name: str, results: dict[str, list[dict]],
             summaries: dict[str, dict]) -> str:
    lines = [f"## 第 {round_name} 轮", ""]
    lines.append("| 后端 | 题数 | 目标分均值 | 目标分中位 | 平均排名 | Top1 | Top5 | 过阈值 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for backend in ("cross_encoder", "jina_listwise"):
        s = summaries[backend]
        lines.append(
            f"| {backend} | {s['cases']} | {s['mean_target_score']:.4f} | "
            f"{s['median_target_score']:.4f} | {s['mean_target_rank']:.2f} | "
            f"{s['top1']} | {s['top5']} | {s['above_threshold']} "
            f"(>= {s['min_score']}) |")
    lines.append("")
    lines.append("| 题 | bge 分 | bge 排名 | jina 分 | jina 排名 | 分差(jina-bge) |")
    lines.append("|---|---|---|---|---|---|")
    bge = {r["id"]: r for r in results["cross_encoder"]}
    jina = {r["id"]: r for r in results["jina_listwise"]}
    for case_id in bge:
        b, j = bge[case_id], jina[case_id]
        lines.append(
            f"| {case_id} | {b['target_score']:.4f} | {b['target_rank']} | "
            f"{j['target_score']:.4f} | {j['target_rank']} | "
            f"{j['target_score'] - b['target_score']:+.4f} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    chunks = build_chunks()
    rounds = {
        "1": (load_cases(EVAL_SETS / "二课表格口语题.json"), []),
        "2": (load_cases(EVAL_SETS / "第二课堂评分表专项.json"), []),
    }
    # 预校验目标短语
    for name, (cases, _) in rounds.items():
        for case in cases:
            target_chunk(case["id"], chunks)

    all_results: dict[str, dict[str, list[dict]]] = {}
    all_summaries: dict[str, dict[str, dict]] = {}
    for backend in ("cross_encoder", "jina_listwise"):
        switch_backend(backend)
        all_results[backend] = {}
        all_summaries[backend] = {}
        for name, (cases, _) in rounds.items():
            rows = run_round(name, cases, chunks, backend)
            all_results[backend][name] = rows
            all_summaries[backend][name] = summarize(rows, backend)

    for name in rounds:
        payload = {
            "round": name,
            "chunks": len(chunks),
            "results": {b: all_results[b][name] for b in all_results},
            "summaries": {b: all_summaries[b][name] for b in all_summaries},
        }
        path = LOGS / f"rerank_ab_round{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"[saved] {path}", flush=True)

    md = ["# bge vs jina 精排对比（施工后）", ""]
    md.append(f"候选池：doc11+doc12 共 {len(chunks)} 个 chunk；"
              "用当前自然句渲染重新生成，不依赖线上索引。")
    md.append("")
    for name in rounds:
        md.append(markdown(name,
                           {b: all_results[b][name] for b in all_results},
                           {b: all_summaries[b][name] for b in all_summaries}))
    (LOGS / "rerank_ab_summary.md").write_text("\n".join(md), encoding="utf-8")
    print("[saved] logs/rerank_ab_summary.md", flush=True)


if __name__ == "__main__":
    main()
