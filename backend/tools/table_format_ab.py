"""表格文本形态 3 组 A/B：键值+完整表头 / 键值+裸哨兵 / 自然句+裸哨兵。

候选池与问题集同 `rerank_ab_eval.py`；只改表格渲染形态，列对齐、多值展开、
补单位三组保持一致，从而隔离“句式”和“表头行”的贡献。

⚠️ 运行时建议先停后端：要和线上 Jina 抢 6GB 显存。
"""
from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.graph.state import Chunk
from app.ingestion import tables as tables_mod
from app.ingestion.chunker import chunk_text
from app.ingestion.clean import build_normalized_text, page_of_offset
from app.ingestion.loaders.pdf import load_pdf
from app.ingestion.table_render import _escape
from app.ingestion.table_rules import (
    apply_score_units, expand_multi_value_row, infer_score_columns,
)
from app.ingestion.tables import table_atomic_ranges
from app.retrieval import reranker

CORPUS = ROOT / "corpus" / "guet"
EVAL_SETS = ROOT / "backend" / "eval_sets"
LOGS = ROOT / "logs"

DOCS = {
    "doc11": "11_创新创业拓展课程学分认定管理办法_桂电教2026-13号.pdf",
    "doc12": "12_第二课堂成绩单制度实施办法_2025修订_桂电2025-30号.pdf",
}

# case_id -> 必须在目标 chunk 里同时出现的 token（三种形态都能命中）
TARGET_TOKENS = {
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
    "tq-11": ["专利类（第一作者单位为桂林电子科技大学）", "软件著作权"],
    "tq-12": ["文体竞赛", "国家级二等奖"],
    "tq-13": ["党校培训", "5分/期"],
    "tq-14": ["新生入学教育", "2分/期"],
    "tq-15": ["“挑战杯”“国创赛”", "国家级特等奖（金奖）"],
    "tq-16": ["创业实践", "创业实体入驻校创新创业基地或孵化中心"],
    "tq-17": ["企业实训", "围绕企业实战项目"],
    "sc-01": ["参与公益活动", "无偿献血"],
    "sc-02": ["参与公益活动", "捐献造血干细胞"],
    "sc-03": ["大学英语及专业英语水平测试", "大学英语六级"],
    "sc-04": ["技能证书", "通过各级计算机等级考试并获得证书"],
    "sc-05": ["校级学生组织负责人、团委兼职副书记", "10分/学年"],
    "sc-06": ["文体竞赛", "国家级二等奖"],
    "sc-07": ["“挑战杯”“国创赛”", "国家级特等奖（金奖）"],
    "sc-08": ["参加暑期“三下乡”社会实践（含专项）", "国家级立项"],
    "sc-09": ["团支部书记、班长", "3分/学年"],
    "sc-10": ["思想政治与道德修养", "不少于15"],
}

# 少数 case 的通用 token 会命中“获得学分方式”里的同名字样（如 p12 的方式文本
# 也含“大学英语六级”），按形态给更精确的 token。
VARIANT_TOKENS = {
    "tq-03": {
        "A": ["列4：大学英语六级"],
        "B": ["列4：大学英语六级"],
        "C": ["大学英语六级，参考学分 5分/项"],
    },
    "sc-03": {
        "A": ["列4：大学英语六级"],
        "B": ["列4：大学英语六级"],
        "C": ["大学英语六级，参考学分 5分/项"],
    },
}


def serialize_groups_kv(table, *, budget: int, full_header: bool) -> list[str]:
    """旧键值形态；列对齐/多值展开/补单位与自然句形态保持一致。"""
    if not table.rows or table.dropped:
        return []
    ncols = len(table.rows[0])
    prh = [tables_mod.is_projected_row_header(r, ncols) for r in table.rows]
    filled, mask = tables_mod.forward_fill(table.rows, return_mask=True)

    if table.col_headers and len(table.col_headers) == ncols:
        header_names = list(table.col_headers)
    elif len(table.headers) == ncols:
        header_names = list(table.headers)
    else:
        header_names = [None] * ncols
    labels = [h or f"列{i + 1}" for i, h in enumerate(header_names)]
    score_cols = infer_score_columns(table.rows, header_names)

    header_line = tables_mod.GROUP_MARK + (
        "｜".join(labels) if full_header else "")
    body_budget = budget - len(header_line) - 1
    lines: list[str] = []
    for raw_row, filled_row, mask_row, is_prh in zip(
            table.rows, filled, mask, prh):
        if is_prh:
            continue
        values = apply_score_units(filled_row, mask_row, score_cols)
        expanded = expand_multi_value_row(raw_row, header_names)
        for idx, text in expanded.items():
            if idx < len(values):
                values[idx] = text
        pairs = []
        for i, label in enumerate(labels):
            value = values[i].strip() if i < len(values) else ""
            if value:
                pairs.append(f"{label}：{_escape(value)}")
        if pairs:
            lines.append("｜".join(pairs))

    if not lines:
        return []
    groups: list[str] = []
    cur: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if cur and used + cost > body_budget:
            groups.append(header_line + "\n" + "\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += cost
    if cur:
        groups.append(header_line + "\n" + "\n".join(cur))
    return groups


def build_variant(variant: str) -> list[Chunk]:
    original = tables_mod.serialize_groups
    if variant == "A":
        tables_mod.serialize_groups = (
            lambda table, *, budget: serialize_groups_kv(
                table, budget=budget, full_header=True))
    elif variant == "B":
        tables_mod.serialize_groups = (
            lambda table, *, budget: serialize_groups_kv(
                table, budget=budget, full_header=False))
    try:
        chunks: list[Chunk] = []
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
                chunks.append(Chunk(
                    chunk_id=f"{doc_key}:{span.chunk_index}",
                    chunk_index=span.chunk_index,
                    document_id=doc_key,
                    document_name=doc_key,
                    text=span.text,
                    char_start=span.char_start,
                    char_end=span.char_end,
                    page=page_of_offset(slices, span.char_start),
                ))
        return chunks
    finally:
        tables_mod.serialize_groups = original


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def target_chunk(case_id: str, chunks: list[Chunk], variant: str) -> Chunk:
    tokens = VARIANT_TOKENS.get(case_id, {}).get(variant) or TARGET_TOKENS[case_id]
    hits = [c for c in chunks if all(tok in c.text for tok in tokens)]
    if len(hits) != 1:
        raise RuntimeError(
            f"{case_id} 目标 token 命中 {len(hits)} 个 chunk：{tokens}")
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
    if reranker._load_model() is None:
        raise RuntimeError(f"{name} 模型加载失败")
    print(f"[backend] {name} loaded", flush=True)


def run_variant(backend: str, variant: str, chunks: list[Chunk],
                cases: list[dict], round_name: str) -> list[dict]:
    print(f"[{backend}] {round_name} {variant}: {len(cases)} cases x "
          f"{len(chunks)} chunks", flush=True)
    rows = []
    for n, case in enumerate(cases, 1):
        target = target_chunk(case["id"], chunks, variant)
        scores = reranker._predict(case["question"], chunks)
        ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
        rank = next(i + 1 for i, (c, _s) in enumerate(ranked)
                    if c.chunk_id == target.chunk_id)
        score = next(s for c, s in ranked if c.chunk_id == target.chunk_id)
        rows.append({
            "id": case["id"],
            "target_chunk_id": target.chunk_id,
            "target_score": round(float(score), 6),
            "target_rank": rank,
            "candidate_count": len(chunks),
        })
        if n % 5 == 0 or n == len(cases):
            print(f"  {n}/{len(cases)}", flush=True)
    return rows


def summarize(rows: list[dict], min_score: float) -> dict:
    scores = [r["target_score"] for r in rows]
    ranks = [r["target_rank"] for r in rows]
    return {
        "cases": len(rows),
        "min_score": min_score,
        "mean_score": round(statistics.mean(scores), 6),
        "median_score": round(statistics.median(scores), 6),
        "mean_rank": round(statistics.mean(ranks), 3),
        "top1": sum(1 for r in rows if r["target_rank"] == 1),
        "top5": sum(1 for r in rows if r["target_rank"] <= 5),
        "above_threshold": sum(1 for r in rows
                               if r["target_score"] >= min_score),
    }


def compare(variant_rows: dict[str, list[dict]], left: str, right: str) -> dict:
    a = {r["id"]: r for r in variant_rows[left]}
    b = {r["id"]: r for r in variant_rows[right]}
    wins = sum(1 for cid in a if b[cid]["target_score"] > a[cid]["target_score"])
    losses = sum(1 for cid in a if b[cid]["target_score"] < a[cid]["target_score"])
    delta_score = statistics.mean(
        b[cid]["target_score"] - a[cid]["target_score"] for cid in a)
    delta_rank = statistics.mean(
        a[cid]["target_rank"] - b[cid]["target_rank"] for cid in a)
    return {"left": left, "right": right, "right_wins": wins,
            "right_losses": losses, "mean_score_delta": round(delta_score, 6),
            "mean_rank_gain": round(delta_rank, 3)}


def markdown(results: dict, summaries: dict, comparisons: dict,
             chunk_counts: dict) -> str:
    lines = ["# 表格文本形态 3 组 A/B", "",
             "候选池：当前代码重新生成的 doc11+doc12 chunks；"
             "列对齐、多值展开、补单位三组一致。", "",
             "| 变体 | chunk 数 |", "|---|---|"]
    for v, count in chunk_counts.items():
        lines.append(f"| {v} | {count} |")
    for backend, rounds in results.items():
        for round_name, variants in rounds.items():
            lines += ["", f"## {backend} / 第 {round_name} 轮", "",
                      "| 变体 | 目标分均值 | 中位 | 平均排名 | Top1 | Top5 | 过阈值 |",
                      "|---|---|---|---|---|---|---|"]
            for v in ("A", "B", "C"):
                s = summaries[backend][round_name][v]
                lines.append(
                    f"| {v} | {s['mean_score']:.4f} | {s['median_score']:.4f} | "
                    f"{s['mean_rank']:.2f} | {s['top1']}/{s['cases']} | "
                    f"{s['top5']}/{s['cases']} | {s['above_threshold']} "
                    f"(>= {s['min_score']}) |")
            c = comparisons[backend][round_name]
            lines += ["", f"- B→C（自然句相对键值）：赢 {c['C_vs_B']['right_wins']}，"
                          f"输 {c['C_vs_B']['right_losses']}，"
                          f"平均分差 {c['C_vs_B']['mean_score_delta']:+.4f}，"
                          f"平均排名改善 {c['C_vs_B']['mean_rank_gain']:+.2f}",
                      f"- A→B（去掉完整表头行）：赢 {c['B_vs_A']['right_wins']}，"
                      f"输 {c['B_vs_A']['right_losses']}，"
                      f"平均分差 {c['B_vs_A']['mean_score_delta']:+.4f}，"
                      f"平均排名改善 {c['B_vs_A']['mean_rank_gain']:+.2f}"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", default="jina_listwise",
                    help="comma separated: jina_listwise,cross_encoder")
    args = ap.parse_args()
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]

    rounds = {
        "1": load_cases(EVAL_SETS / "二课表格口语题.json"),
        "2": load_cases(EVAL_SETS / "第二课堂评分表专项.json"),
    }
    variants = {}
    for variant in ("A", "B", "C"):
        variants[variant] = build_variant(variant)
        for round_name, cases in rounds.items():
            for case in cases:
                target_chunk(case["id"], variants[variant], variant)
        print(f"[variant {variant}] chunks={len(variants[variant])}", flush=True)

    results: dict = {}
    summaries: dict = {}
    comparisons: dict = {}
    for backend in backends:
        switch_backend(backend)
        min_score = float(reranker._pcfg("min_score", 0.3))
        results[backend] = {}
        summaries[backend] = {}
        comparisons[backend] = {}
        for round_name, cases in rounds.items():
            variant_rows = {}
            for variant in ("A", "B", "C"):
                variant_rows[variant] = run_variant(
                    backend, variant, variants[variant], cases, round_name)
            results[backend][round_name] = variant_rows
            summaries[backend][round_name] = {
                v: summarize(rows, min_score)
                for v, rows in variant_rows.items()
            }
            comparisons[backend][round_name] = {
                "C_vs_B": compare(variant_rows, "B", "C"),
                "B_vs_A": compare(variant_rows, "A", "B"),
            }

    payload = {
        "chunk_counts": {v: len(c) for v, c in variants.items()},
        "results": results,
        "summaries": summaries,
        "comparisons": comparisons,
    }
    out_json = LOGS / "table_format_ab.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    md = markdown(results, summaries, comparisons,
                  {v: len(c) for v, c in variants.items()})
    out_md = LOGS / "table_format_ab_summary.md"
    out_md.write_text(md, encoding="utf-8")
    print(md)
    print(f"[saved] {out_json}")
    print(f"[saved] {out_md}")


if __name__ == "__main__":
    main()
