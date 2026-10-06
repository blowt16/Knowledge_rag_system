"""题库完整性（M5-1）—— **每一题的答案都要能在活库正文里找到**。

为什么这条最重要：
    题库是消融实验与所有评测指标的地基。题面错、答案错、指向的文档不存在，
    跑出来的表照样有数字，只是数字没有意义 —— 而且**完全看不出来**。
    所以逐题回**活库**校验，而不是信任生成时的自检。

样本由 `backend/tools/make_eval_cases.py` 生成（模型出题 + 生成期校验），
本文件是**第二道**独立校验。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import db
from app.core.config import repo_path

FIXTURE = repo_path("backend", "tests", "fixtures", "eval_cases_v1.json")

CASES: list[dict] = []
if FIXTURE.exists():
    CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

pytestmark = pytest.mark.skipif(not CASES, reason="题库未生成")


def _squeeze(s: str) -> str:
    """去空白比对 —— 与生成器同一口径（见 make_eval_cases._squeeze）。"""
    return re.sub(r"\s+", "", s or "")


async def test_case_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "用例 id 有重复"


async def test_case_types_and_suites_are_in_contract():
    """取值必须在 §4.1 的契约里 —— 库里 CHECK 约束也是这几个值。"""
    types = {c["case_type"] for c in CASES}
    assert types <= {"factual", "cross_paragraph", "doc_number", "refusal",
                     "restricted", "multi_turn"}, types
    suites = {c.get("suite", "full") for c in CASES}
    assert suites <= {"full", "refusal_calib"}, suites


async def test_doc_number_cases_only_reference_first_five_docs():
    """★ 文号题**只能出 01–05**（§6.9 K-1 附记）。

    06–10 五份的本文文号在源文件里根本不存在（红头被裁掉了，渲染确认过像素），
    出这几份的文号题就是**注定失败**的题 —— 会让检索基线凭空掉一截，
    而且看起来像"检索坏了"。
    """
    bad = [c["id"] for c in CASES
           if c["case_type"] == "doc_number"
           and not str(c.get("expected_document", "")).startswith(("01_", "02_", "03_", "04_", "05_"))]
    assert bad == [], f"文号题指向了 06–10：{bad}"


async def test_ground_truth_is_verbatim_in_live_corpus():
    """★★ 核心断言：每一条 ground_truth 都要在**活库正文**里逐字找到。

    ⚠️ 忽略空白比对（与生成器同口径）：规范化正文里有 PDF 提取留下的硬换行，
       而模型复述时写成一行 —— 那是同一句话，不是改写。
    """
    async with db.tx() as conn:
        rows = await conn.fetch(
            "SELECT title, normalized_text_path FROM documents WHERE status='active'")
    texts: dict[str, str] = {}
    for r in rows:
        p = Path(r["normalized_text_path"])
        if not p.suffix:
            p = p.with_suffix(".txt")
        texts[r["title"]] = (p.read_text(encoding="utf-8", errors="ignore")
                             if p.exists() else "")

    # 受限题的源文档**按设计不在语料里**（评测自建自清，见负责人 2026-10-06 的决定），
    # 所以它们对着自己的源文件校验，而不是对着活库。
    restricted_src = (repo_path("backend", "tests", "fixtures", "restricted_sample.md")
                      .read_text(encoding="utf-8"))

    problems: list[str] = []
    checked = restricted_checked = 0
    for c in CASES:
        gt = c.get("ground_truth")
        if not gt:
            continue
        if c["case_type"] == "restricted":
            if _squeeze(gt) not in _squeeze(restricted_src):
                problems.append(f"{c['id']}: 受限题答案不在 restricted_sample.md 里")
            else:
                restricted_checked += 1
            continue
        title = c.get("expected_document")
        if title not in texts:
            problems.append(f"{c['id']}: 期望文档不在库里 —— {title}")
            continue
        if _squeeze(gt) not in _squeeze(texts[title]):
            problems.append(f"{c['id']}: 答案不是《{title[:20]}》的原文片段")
            continue
        checked += 1

    assert problems == [], "题库有问题：\n" + "\n".join(problems)
    assert checked >= 40, f"只校验到 {checked} 题，太少（题库缩水了？）"
    assert restricted_checked >= 10, f"受限题只校验到 {restricted_checked} 道"


async def test_refusal_cases_have_no_ground_truth():
    """拒答题的 `ground_truth` 必须为空 —— 有答案就不叫拒答题了。"""
    bad = [c["id"] for c in CASES
           if c["case_type"] == "refusal" and c.get("ground_truth")]
    assert bad == [], bad


async def test_calib_suite_has_both_answerable_and_unanswerable():
    """★ 校准小集必须**两种题都有**（§5.2 B 组）。

    B 组四项里，漏答率看「有据题被判拒答」、误答率看「无据题被判作答」——
    只放一种，这两项里必有一项**算不出来**（分母为 0）。
    """
    calib = [c for c in CASES if c.get("suite") == "refusal_calib"]
    assert 10 <= len(calib) <= 15, f"校准小集应有 10–15 题，实际 {len(calib)}"

    has_gt = [c for c in calib if c.get("ground_truth")]
    no_gt = [c for c in calib if not c.get("ground_truth")]
    assert has_gt, "校准小集里一道有据题都没有 → 漏答率算不出来"
    assert no_gt, "校准小集里一道无据题都没有 → 误答率算不出来"


async def test_multiturn_cases_carry_turns():
    """多轮题必须带 `turns`，且每一轮都有路由/澄清标注。"""
    for c in CASES:
        if c["case_type"] != "multi_turn":
            continue
        turns = c.get("turns")
        assert turns, f"{c['id']} 没有 turns"
        for t in turns:
            assert t.get("expected_route") in ("chat", "clarify", "knowledge")
            assert t.get("should_clarify") in (0, 1)
