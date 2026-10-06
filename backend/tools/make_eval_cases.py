"""生成评测题库（M5-1）—— 让模型出题，但**答案必须逐字来自原文**。

    cd backend && uv run python tools/make_eval_cases.py [--limit N]

产物：`backend/tests/fixtures/eval_cases_v1.json`

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


def _pronoun_words() -> set[str]:
    """代词表 —— **取自 app.yaml 的 `rules.pronoun_words`**，与 resolve 节点同源。

    ⚠️ 不另抄一份：抄了就会漂移，而漂移之后「生成器认为没问题、图却判澄清」，
       两边对着干还查不出原因。
    """
    import yaml
    cfg_path = REPO / "backend" / "app" / "config" / "app.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return set(data["rules"]["pronoun_words"])


def _has_pronoun(question: str, pronouns: set[str] | None = None) -> bool:
    """单轮题里**不许出现代词**。

    实测教训：首版没用这条，生成了一批「这个细则管的是哪些学生啊？」——
    单轮评测没有上文，「这个」指不明白，**图判澄清是完全正确的行为**，
    于是评测文件里凭空多出几条「路由错」的假失败。
    代词的活儿是多轮题库（eval_multiturn）在干的。
    """
    import jieba
    return bool(set(jieba.lcut(question)) & (pronouns if pronouns is not None
                                             else _pronoun_words()))


def _squeeze(s: str) -> str:
    """去掉**所有空白**再比。用于「逐字」校验。

    ⚠️ 必须这么做：规范化正文里有 PDF 提取留下的硬换行与空行
    （实测：`…提出申请并经\\n\\n学院审核同意后送达；`），模型复述时自然写成
    一行 —— 用带空白的原串去比会判成「不是原文」，而**它确实是原文**。
    去空白比对仍是强保证：**非空白字符序列必须连续出现在原文里**，
    改写、概括、跨段拼接都会当场露馅。
    """
    return re.sub(r"\s+", "", s)


async def _ask(chunk: str, kind: str, title: str, retries: int = 2) -> dict | None:
    """让模型围绕给定片段出一道题。答案必须是原文片段（逐字）。"""
    if kind == "factual":
        instruction = ("出一道路人式的事实题：问某项规定/条件/时限是**什么**。"
                       "问法要像一个学生随口问的，不要照抄原文句式。")
    elif kind == "cross_paragraph":
        instruction = ("出一道需要**把这一整段材料里的两处信息合起来**才能回答的题"
                       "（例如把适用对象与时限结合）。")
    else:
        raise ValueError(kind)

    prompt = (
        f"下面是《{title}》里的一段材料：\n\n\"\"\"\n{chunk}\n\"\"\"\n\n"
        f"{instruction}\n\n"
        "严格要求：\n"
        "1. `ground_truth` 必须是上面材料里**连续的一段原文**，一字不改（含标点）。\n"
        "2. 不许用自己的话概括，不许跨出这段材料。\n"
        "3. **问句里不许出现代词**（这个 / 那个 / 该 / 其 / 上述 …）—— "
        "这是**单轮**提问，没有上文，代词会让问题指代不明。"
        "请用「本办法」「这份文件」这类自足的说法，或直接点出文种。\n"
        "4. 只输出 JSON：{\"question\": \"...\", \"ground_truth\": \"...\"}\n\n"
        # ★ few-shot 是决定性的：不给例子时模型爱改写（首轮实测 50 段里只通过 19），
        #   给了例子之后通过率大幅上升 —— 这类「照抄」任务上，一个例子顶十条规则。
        "示例（假设材料里写着「学生应当在考试前向所在学院提出申请，经批准后方可缓考。」）：\n"
        "{\"question\": \"缓考要提前跟谁说、什么时候说？\", "
        "\"ground_truth\": \"学生应当在考试前向所在学院提出申请，经批准后方可缓考。\"}\n"
        "注意答案**逐字**就是材料里的那句话，一个字都没改。"
    )
    for _ in range(retries + 2):
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": prompt}], timeout=45, max_tokens=800)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        q = str(data.get("question", "")).strip()
        gt = str(data.get("ground_truth", "")).strip()
        # ★ 硬校验一：答案必须是这段材料的逐字子串（忽略空白，见 `_squeeze`）
        if not (q and gt and _squeeze(gt) in _squeeze(chunk) and len(_squeeze(gt)) >= 10):
            continue
        # ★ 硬校验二：单轮题**不许带代词**（见 `_has_pronoun` 的实测教训）。
        #   带代词会被图的 resolve 判成「指代不明」→ 走 clarify，那是**正确行为**，
        #   但会让评测里凭空多出「路由错」的假失败。
        if _has_pronoun(q):
            continue        # 外层循环重试（prompt 里已明令不许用代词）
        return {"question": q, "ground_truth": gt}
    return None


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
