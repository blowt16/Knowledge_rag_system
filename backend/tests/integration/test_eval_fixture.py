"""题库的 ground truth 与**真实库**对得上（回归锁，需要 PG + 已入库语料）。

⚠️ 为什么必须连库查：`expected_document` 写的是**文档标题**（M1 定案：id 每次
   重新入库都会变，标题不会）。代价是标题一旦对不上，评测不会报错 ——
   所有题都判「未命中」，指标掉到 0，而人只会以为「检索变差了」。

⚠️ 第二条更隐蔽：`resolved_must_contain_any` 里的锚点词是**手工核对**过
   「该词确实出现在这份文档里」的。若语料换了，锚点失效，消解判据就会
   把一个**正确**的消解判成错误。这里把核对结论固化成断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "eval_multiturn.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


async def _active_docs() -> dict[str, str]:
    """标题 → 规范化文本路径（只取 active）。"""
    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT title, normalized_text_path FROM documents WHERE status = 'active'"
        )
    return {r["title"]: r["normalized_text_path"] for r in rows}


async def test_expected_documents_exist_and_are_active(payload):
    docs = await _active_docs()
    missing = sorted({
        t["expected_document"]
        for c in payload["cases"] for t in c["turns"]
        if t["expected_document"] and t["expected_document"] not in docs
    })
    assert not missing, (
        "题库里的 ground truth 标题在库里不存在（active）：\n  "
        + "\n  ".join(missing)
        + f"\n库里有：{sorted(docs)}"
    )


async def test_anchor_words_actually_appear_in_that_document(payload):
    """锚点词必须真的在那份文档的正文里 —— 否则消解判据会误判。"""
    docs = await _active_docs()
    problems: list[str] = []

    for case in payload["cases"]:
        for i, turn in enumerate(case["turns"], start=1):
            anchors = turn["resolved_must_contain_any"] or []
            title = turn["expected_document"]
            if not anchors or not title:
                continue
            path = docs.get(title)
            if not path:
                continue  # 上一条用例已经报过「标题不存在」
            text = Path(path).read_text(encoding="utf-8")
            if not any(a in text for a in anchors):
                problems.append(
                    f"{case['id']} 第 {i} 轮：锚点 {anchors} 在《{title}》正文里一个都没有"
                )

    assert not problems, "\n".join(problems)
