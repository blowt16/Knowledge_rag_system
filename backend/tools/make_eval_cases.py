"""生成评测题库（M5-1）—— 让模型出题，但**答案必须逐字来自原文**。

    cd backend && uv run python tools/make_eval_cases.py [--limit N]

产物：`backend/tests/fixtures/eval_cases_v1.json`

⚠️ 出题 prompt 与两道硬校验（答案逐字来自原文 / 问句不带代词）在
   `app/eval/generate.py` —— **与界面上的「从文档自动生成」是同一份**，
   本文件不另抄一份。

## 为什么不是手写

60–80 题手写要几小时，而且人写的"标准答案"很容易与原文有细微出入（多一个
「的」、少了标点），评测时就成了噪声。这里反过来：**先取原文片段，再让模型
围绕它出题**，答案直接从原文抄 —— 生成期校验一次（必须是子串），
测试期再对着活库正文校验一次（`tests/integration/test_eval_cases.py`）。

## 出题口径（都来自方案的硬约束）

| 类型 | 占比 | 约束 |
|---|---|---|
| `factual` 事实型 | 55% | 单段可答 |
| `cross_paragraph` 跨段落 | 5% | 答案分散在相邻两段 |
| `doc_number` 文号 | 20% | **只能出 01–05 五份**（§6.9 K-1 附记：06–10 正文里没有本文文号，出了就是注定失败的题） |
| `refusal` 拒答 | 10% | 校园里真实会问、但这 10 份公文里查不到 |
| `multi_turn` 多轮 | 10% | 由既有 `eval_multiturn.json` 迁移，**不在这里生成** |

另有 `case_type='restricted'` 的受限题 —— 它需要一份受限文档，
按负责人 2026-10-06 的决定「**评测自建自清**」，所以也**不在这里生成**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.core import llm  # noqa: E402
from app.core.config import repo_path  # noqa: E402
from app.eval import generate  # noqa: E402
from app.ingestion.chunker import chunk_text  # noqa: E402

OUT = REPO / "backend" / "tests" / "fixtures" / "eval_cases_v1.json"

# 文号题只出这几份（见模块头的 K-1 附记）
DOC_NUMBER_DOCS = ("01_", "02_", "03_", "04_", "05_")

# 拒答题：校园里真实会问、但这批公文覆盖不到 —— 每题都要能说出「库里确实没有」的理由
REFUSAL_SEEDS = [
    ("图书馆的开放时间是几点？到晚上几点关门？", "馆舍开放时间，这 10 份都是学籍/考试/参军类公文"),
    ("学校食堂有哪些窗口？营业到几点？", "后勤餐饮，语料完全不含"),
    ("宿舍几点断电？可以用电磁炉吗？", "宿舍管理规定，语料不含"),
    ("校园网怎么开通？密码忘了怎么办？", "信息化服务，语料不含"),
    ("校车班次时刻表在哪里查？", "交通后勤，语料不含"),
    ("学费什么时候交？可以分期吗？", "财务收费，语料不含"),
    ("体育课的选课规则是什么？", "体育教学，语料里只有考试与学籍相关"),
    ("考研的报名流程是怎样的？", "研究生招生，语料不含（只有保研/推免字样，无流程）"),
    ("学校有哪些社团？怎么申请成立新社团？", "学生社团管理，语料不含"),
    ("校医院的接诊时间是？医保怎么报销？", "医疗服务，语料不含"),
    ("机动车进校园怎么报备？停车费多少？", "保卫处管理，语料不含"),
    ("教师职称评审的条件是什么？", "人事，且语料全是学生侧"),
]


# ⚠️ `squeeze` / `has_pronoun` / 出题 prompt 与两道硬校验**都从 `app.eval.generate` 引用**，
#    这里不再各留一份。抄一份回去迟早会漂，而漂移之后「脚本能过、界面过不了」
#    或者反过来，两边对着干还查不出原因 —— 本仓库已经为这类事踩过坑。
_squeeze = generate.squeeze
_has_pronoun = generate.has_pronoun


_INSTRUCTIONS = {
    "factual": generate.FACTUAL_INSTRUCTION,
    "cross_paragraph": ("出一道需要**把这一整段材料里的两处信息合起来**才能回答的题"
                        "（例如把适用对象与时限结合）。"),
}


async def _ask(chunk: str, kind: str, title: str) -> dict | None:
    """围绕给定片段出一道题。prompt 与两道硬校验在 `app.eval.generate` 里。"""
    return await generate.ask_model_for_case(chunk, title, _INSTRUCTIONS[kind])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 份文档（调试用）")
    args = ap.parse_args()

    from app import db
    await db.init_pool()
    try:
        async with db.tx() as conn:
            docs = await conn.fetch(
                "SELECT id, title, normalized_text_path FROM documents "
                "WHERE status='active' ORDER BY title")
    finally:
        await db.close_pool()

    if args.limit:
        docs = docs[:args.limit]

    rng = random.Random(20261006)
    cases: list[dict] = []
    stats: dict[str, int] = {}

    for doc in docs:
        title = doc["title"]
        text_path = Path(doc["normalized_text_path"])
        if not text_path.suffix:
            text_path = text_path.with_suffix(".txt")
        if not text_path.exists():
            print(f"!! 缺规范化正文：{title}", file=sys.stderr)
            continue
        text = text_path.read_text(encoding="utf-8", errors="ignore")
        chunks = [c.text if hasattr(c, "text") else c for c in chunk_text(text)]
        chunks = [c for c in chunks if len(c.strip()) >= 120]
        if not chunks:
            continue

        # 事实型：每份挑 5 段（均匀分布，避开重复）
        picked = rng.sample(range(len(chunks)), min(5, len(chunks)))
        picked.sort()
        for i, idx in enumerate(picked):
            got = await _ask(chunks[idx], "factual", title)
            if not got:
                continue
            cid = f"f-{title[:2]}-{i + 1:02d}"
            cases.append({
                "id": cid, "case_type": "factual", "suite": "full",
                "question": got["question"], "ground_truth": got["ground_truth"],
                "expected_document": title,
                "expected_route": "knowledge", "should_clarify": 0,
            })
            stats["factual"] = stats.get("factual", 0) + 1

        # 跨段落：整份只出 1 道，且只给 2 份文档（占比 5%）
        if title.startswith(("03_", "04_")) and len(chunks) >= 3:
            got = await _ask(chunks[len(chunks) // 2], "cross_paragraph", title)
            if got:
                cases.append({
                    "id": f"x-{title[:2]}", "case_type": "cross_paragraph", "suite": "full",
                    "question": got["question"], "ground_truth": got["ground_truth"],
                    "expected_document": title,
                    "expected_route": "knowledge", "should_clarify": 0,
                })
                stats["cross_paragraph"] = stats.get("cross_paragraph", 0) + 1

        # 文号题：只出 01–05，问「某文号是关于什么的」
        if title.startswith(DOC_NUMBER_DOCS):
            m = re.search(r"桂电[^\s_]*(\d{4})[-–—]?(\d+)\s*号", title)
            num = None
            if m:
                num = f"桂电教〔{m.group(1)}〕{m.group(2)}号"
            if num:
                gt = chunks[0][:160]
                cases.append({
                    "id": f"n-{title[:2]}", "case_type": "doc_number", "suite": "full",
                    "question": f"{num}这份文件是关于什么的？",
                    "ground_truth": gt,
                    "expected_document": title,
                    "expected_route": "knowledge", "should_clarify": 0,
                })
                stats["doc_number"] = stats.get("doc_number", 0) + 1

    # 多轮指代：**直接迁移** M2 建好的题库（15 段 / 27 轮，逐轮四标注都在），
    # 不重新生成 —— 它已经被 M2 的验收与回归检查验证过。
    mt_path = REPO / "backend" / "tests" / "fixtures" / "eval_multiturn.json"
    if mt_path.exists():
        mt = json.loads(mt_path.read_text(encoding="utf-8"))
        for conv in mt.get("cases", []):
            cases.append({
                "id": f"mt-{conv['id']}", "case_type": "multi_turn", "suite": "full",
                "question": conv["turns"][0]["question"],
                "ground_truth": None,
                "expected_document": conv["turns"][0].get("expected_document"),
                "expected_route": conv["turns"][0].get("expected_route", "knowledge"),
                "should_clarify": conv["turns"][0].get("should_clarify", 0),
                "turns": conv["turns"],
                "note": f"迁移自 eval_multiturn.json::{conv['id']}",
            })
            stats["multi_turn"] = stats.get("multi_turn", 0) + 1

    # 拒答题（固定种子，不需要模型 —— 它们的价值恰恰是「库里没有」）
    for i, (q, why) in enumerate(REFUSAL_SEEDS, start=1):
        cases.append({
            "id": f"r-{i:02d}", "case_type": "refusal", "suite": "full",
            "question": q, "ground_truth": None, "expected_document": None,
            "expected_route": "knowledge", "should_clarify": 0,
            "note": f"库中确实没有：{why}",
        })
        stats["refusal"] = stats.get("refusal", 0) + 1

    # ---- 校准小集（§5.2 B 组四项指标用，10–15 题）------------------------
    # 必须是**有据题与无据题都有**：B 组同时看漏答率（有据被判拒答）与
    # 误答率（无据被判作答），只放一种就有一半指标算不出来。
    calib: list[str] = [c["id"] for c in cases if c["case_type"] == "refusal"]
    calib += [c["id"] for c in cases if c["case_type"] == "factual"][:3]
    calib = calib[:15]
    for c in cases:
        if c["id"] in calib:
            c["suite"] = "refusal_calib"
    stats["refusal_calib"] = len(calib)

    payload = {
        "version": 1,
        "note": "M5-1 题库（由 tools/make_eval_cases.py 生成）。"
                "ground_truth 一律是原文逐字片段，测试期再对活库正文校验一次。",
        "cases": cases,
        "stats": stats,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {OUT.name}：共 {len(cases)} 题 {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
