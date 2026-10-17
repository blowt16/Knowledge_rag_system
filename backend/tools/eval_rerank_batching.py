"""对比 Jina listwise 分批精排与单次全量精排：分数变化 + 性能。

用法（**必须先停后端**，否则两个 Jina 会抢显存）：
    cd backend && ../.venv/Scripts/python.exe tools/eval_rerank_batching.py
    cd backend && ../.venv/Scripts/python.exe tools/eval_rerank_batching.py --batch 10 --limit 5

对每条评测题：
  1. 走与图一致的检索链路（向量 + BM25 → weighted_rrf）拿到候选；
  2. 同一批候选分别跑 `batch_candidates=0`（基线，一次全量前向）
     和 `batch_candidates=N`（分批）；
  3. 对比逐 chunk 分数差、Spearman 秩相关、Top-1/Top-5 是否变化、期望文档是否命中；
  4. 记录各自耗时与 `torch.cuda.max_memory_allocated`。

⚠️ 基线在 20+ 候选时可能几十秒一条；全量 17 题请留足时间。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import torch

from app import db
from app.core.deps import UserContext
from app.retrieval import reranker
from app.retrieval.fusion import weighted_rrf
from app.retrieval.search import bm25_retrieve, vector_retrieve


def _spearman(a: list[float], b: list[float]) -> float:
    """无并列分数的 Spearman 秩相关（本评测的模型分数基本不重复）。"""
    n = len(a)
    if n < 2:
        return 1.0
    ra = [0] * n
    rb = [0] * n
    for rank, i in enumerate(sorted(range(n), key=lambda i: a[i], reverse=True), 1):
        ra[i] = rank
    for rank, i in enumerate(sorted(range(n), key=lambda i: b[i], reverse=True), 1):
        rb[i] = rank
    d2 = sum((x - y) ** 2 for x, y in zip(ra, rb))
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def _top_ids(chunks, scores, min_score: float, k: int) -> list[str]:
    ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
    return [c.chunk_id for c, s in ranked if s >= min_score][:k]


def _raw_top_ids(chunks, scores, k: int) -> list[str]:
    """不套 min_score 的原始排名 —— 用来区分「模型排序变了」和「只是跨过阈值」。"""
    ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
    return [c.chunk_id for c, _s in ranked[:k]]


def _predict_with_batch(question: str, chunks, batch: int, orig_pcfg):
    """临时把 `batch_candidates` 固定为 `batch`，其余配置照旧。"""
    def patched(key, default=None):
        if key == "batch_candidates":
            return batch
        return orig_pcfg(key, default)

    reranker._pcfg = patched
    try:
        return reranker._predict(question, chunks)
    finally:
        reranker._pcfg = orig_pcfg


def _measure(question: str, chunks, batch: int, orig_pcfg):
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    scores = _predict_with_batch(question, chunks, batch, orig_pcfg)
    torch.cuda.synchronize()
    return {
        "scores": scores,
        "seconds": time.perf_counter() - started,
        "peak_vram_gb": torch.cuda.max_memory_allocated() / 1024 ** 3,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="eval_sets/二课表格口语题.json")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0=全部")
    parser.add_argument("--role", default="student")
    parser.add_argument("--out", default="logs/rerank_batching_eval.json")
    args = parser.parse_args()

    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = BACKEND_DIR / fixture
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[:args.limit]

    if not torch.cuda.is_available():
        print("CUDA 不可用，无法比较")
        return 1
    free, total = torch.cuda.mem_get_info()
    print(f"GPU free {free / 1024 ** 3:.2f} / {total / 1024 ** 3:.2f} GB")
    if free < 2 * 1024 ** 3:
        print("⚠️ 空闲显存不足 2GB —— 请先停后端，否则模型加载会颠簸/OOM")

    orig_pcfg = reranker._pcfg
    top_k = 5
    min_score = float(reranker._pcfg("min_score", 0.1))
    user = UserContext(id="eval", username="eval", role=args.role, token_version=0)
    results = []

    # ⚠️ 先连库、再加载 CUDA 模型。反过来的话，asyncpg 在 Windows 上解析 DSN 时
    #    会调 ctypes 的 SHGetFolderPathW，实测在 CUDA 初始化后直接 access violation。
    await db.init_pool()
    try:
        print("加载 Jina 模型 ...")
        if not reranker.preload():
            print("模型加载失败")
            return 1

        for idx, case in enumerate(cases, 1):
            question = case["question"]
            async with db.tx() as conn:
                vec = await vector_retrieve(conn, question, user)
                bm = await bm25_retrieve(conn, question, user)
            fused = weighted_rrf([
                ({"text": question, "target": "vector", "weight": 1.0}, vec),
                ({"text": question, "target": "bm25", "weight": 1.0}, bm),
            ])
            if not fused:
                print(f"[{idx}/{len(cases)}] {case['id']} 无候选，跳过")
                continue

            # 先跑分批（快），再跑基线（慢）—— 基线跑完 GPU 已热，不占分批便宜
            try:
                batched = _measure(question, fused, args.batch, orig_pcfg)
            except Exception as e:  # noqa: BLE001
                torch.cuda.empty_cache()
                print(f"[{idx}/{len(cases)}] {case['id']} 分批也失败：{type(e).__name__}: {e}")
                continue
            try:
                baseline = _measure(question, fused, 0, orig_pcfg)
            except Exception as e:  # noqa: BLE001
                torch.cuda.empty_cache()
                print(f"[{idx}/{len(cases)}] {case['id']} 基线 OOM/失败，"
                      f"只记录分批结果：{type(e).__name__}")
                results.append({
                    "id": case["id"], "question": question,
                    "candidates": len(fused),
                    "batched_seconds": round(batched["seconds"], 2),
                    "batched_peak_vram_gb": round(batched["peak_vram_gb"], 2),
                    "baseline_error": type(e).__name__,
                })
                continue

            b_scores, u_scores = batched["scores"], baseline["scores"]
            deltas = [abs(b - u) for b, u in zip(b_scores, u_scores)]
            b_top = _top_ids(fused, b_scores, min_score, top_k)
            u_top = _top_ids(fused, u_scores, min_score, top_k)
            b_raw = _raw_top_ids(fused, b_scores, top_k)
            u_raw = _raw_top_ids(fused, u_scores, top_k)
            b_top1 = b_raw[0] if b_raw else None
            u_top1 = u_raw[0] if u_raw else None
            expected_docs = set(case.get("expected_doc_ids") or [])
            by_id = {c.chunk_id: c for c in fused}

            def _coverage(ids: list[str]) -> bool:
                return any(by_id[i].document_id in expected_docs for i in ids)

            row = {
                "id": case["id"],
                "question": question,
                "candidates": len(fused),
                "batched_seconds": round(batched["seconds"], 2),
                "baseline_seconds": round(baseline["seconds"], 2),
                "batched_peak_vram_gb": round(batched["peak_vram_gb"], 2),
                "baseline_peak_vram_gb": round(baseline["peak_vram_gb"], 2),
                "mean_abs_score_delta": round(sum(deltas) / len(deltas), 5),
                "max_abs_score_delta": round(max(deltas), 5),
                "spearman": round(_spearman(b_scores, u_scores), 4),
                # 原始排名（不套阈值）：模型排序本身变了多少
                "raw_top1_same": b_top1 == u_top1,
                "raw_top5_overlap": len(set(b_raw) & set(u_raw)) / max(len(u_raw), 1),
                "raw_batched_coverage": _coverage(b_raw),
                "raw_baseline_coverage": _coverage(u_raw),
                # 实际链路（套 min_score）：阈值把多少候选卡掉了
                "batched_kept": len(b_top),
                "baseline_kept": len(u_top),
                "top1_same": b_top[0] == u_top[0] if b_top and u_top else b_top == u_top,
                "top5_overlap": len(set(b_top) & set(u_top)) / max(len(u_top), 1),
                "batched_coverage": _coverage(b_top),
                "baseline_coverage": _coverage(u_top),
                "scores": [
                    {"chunk_id": c.chunk_id, "document_id": c.document_id,
                     "batched": round(b, 5), "baseline": round(u, 5)}
                    for c, b, u in zip(fused, b_scores, u_scores)
                ],
            }
            results.append(row)
            print(f"[{idx}/{len(cases)}] {case['id']} n={len(fused)} "
                  f"batch={row['batched_seconds']}s/unbatch={row['baseline_seconds']}s "
                  f"Δmean={row['mean_abs_score_delta']} ρ={row['spearman']} "
                  f"top5={row['top5_overlap']:.0%} "
                  f"cover={row['batched_coverage']}/{row['baseline_coverage']}")
    finally:
        await db.close_pool()

    if not results:
        print("没有可评测的样本")
        return 1

    compared = [r for r in results if "baseline_error" not in r]
    n = len(compared)
    if not n:
        print("所有基线都 OOM/失败，只有分批结果")
        n = 1
    summary = {
        "cases": len(results),
        "compared": len(compared),
        "baseline_failures": len(results) - len(compared),
        "batch": args.batch,
        "mean_batched_seconds": round(sum(r["batched_seconds"] for r in results) / len(results), 2),
        "mean_baseline_seconds": round(sum(r["baseline_seconds"] for r in compared) / n, 2),
        "mean_speedup": round(sum(r["baseline_seconds"] for r in compared) /
                              max(sum(r["batched_seconds"] for r in compared), 1e-9), 2),
        "mean_abs_score_delta": round(sum(r["mean_abs_score_delta"] for r in compared) / n, 5),
        "mean_spearman": round(sum(r["spearman"] for r in compared) / n, 4),
        "raw_top1_same_rate": round(sum(r["raw_top1_same"] for r in compared) / n, 3),
        "mean_raw_top5_overlap": round(sum(r["raw_top5_overlap"] for r in compared) / n, 3),
        "raw_batched_coverage": sum(r["raw_batched_coverage"] for r in compared),
        "raw_baseline_coverage": sum(r["raw_baseline_coverage"] for r in compared),
        "top1_same_rate": round(sum(r["top1_same"] for r in compared) / n, 3),
        "mean_top5_overlap": round(sum(r["top5_overlap"] for r in compared) / n, 3),
        "batched_coverage": sum(r["batched_coverage"] for r in compared),
        "baseline_coverage": sum(r["baseline_coverage"] for r in compared),
        "max_batched_seconds": max(r["batched_seconds"] for r in results),
        "max_baseline_seconds": (max(r["baseline_seconds"] for r in compared)
                                 if compared else None),
    }
    print("\n=== 汇总 ===")
    for k, v in summary.items():
        print(f"{k:>24}: {v}")
    print("\n逐条明细:")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
