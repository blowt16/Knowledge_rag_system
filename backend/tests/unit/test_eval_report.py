"""评测报告的口径（§7）—— 算式、达标线、缺项。

**为什么口径要在服务端**（§7.1）：与 `config_label` 同一个理由 —— 指标会演进，
前端拼公式就会出现「同一份数据算出两个分」。所以这里逐项验算，
前端只负责显示。

**为什么缺项不给数字**（决策 13）：拿不齐的数凑平均值，比不给数更误导 ——
它看起来像个结论。三项平均冒充综合分，是最容易被截图当结论发出去的那种错。

⚠️ 别和「四项都拿到了分、且真的都是 0」混为一谈 —— **那是 0，不是缺项**。
"""

from __future__ import annotations

import pytest

from app.eval import report

RAGAS_KEYS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")

THRESHOLDS = {"context_recall": 0.7, "context_precision": 0.6,
              "faithfulness": 0.8, "answer_relevancy": 0.7}


def _run(*, means=None, available=True, errors=None) -> dict:
    return {"metrics": {"ragas": means or {}, "ragas_available": available,
                        "ragas_errors": errors or []},
            "status": "done", "error": None,
            "started_at": None, "finished_at": None}


def _case(case_id="c1", *, answer="答案", question="问题", gt="标准答案",
          error=None, **ragas) -> dict:
    metrics = {f"ragas_{k}": v for k, v in ragas.items() if v is not None}
    return {"case_id": case_id, "question": question, "ground_truth": gt,
            "answer": answer, "error": error, "metrics": metrics}


# ============================================================
# 算式（§7.2）
# ============================================================

def test_card_scores_are_means_over_scored_cases_only():
    """四张卡 = 该项在**所有算出了分的题**上的算术平均。

    参考图的验算：召回列 (1+1+0+1+1)/5 = 0.8。
    """
    cases = [
        _case("a", context_recall=1.0, context_precision=0.7, faithfulness=1.0,
              answer_relevancy=0.9951),
        _case("b", context_recall=1.0, context_precision=0.7, faithfulness=1.0,
              answer_relevancy=0.9951),
        _case("c", context_recall=0.0, context_precision=0.7, faithfulness=1.0,
              answer_relevancy=0.9951),
        _case("d", context_recall=1.0, context_precision=0.7, faithfulness=1.0,
              answer_relevancy=0.9951),
        _case("e", context_recall=1.0, context_precision=0.7, faithfulness=1.0,
              answer_relevancy=0.9951),
    ]
    out = report.build(_run(means={k: 0.5 for k in RAGAS_KEYS}), cases)
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["context_recall"]["score"] == pytest.approx(0.8)
    assert cards["context_precision"]["score"] == pytest.approx(0.7)
    # 综合得分 = 四张卡的算术平均；(0.8+0.7+1+0.9951)/4
    assert out["report"]["composite_score"] == pytest.approx(
        round((0.8 + 0.7 + 1.0 + 0.9951) / 4, 4))


def test_per_case_score_is_the_mean_of_its_four():
    """逐题均分 = 该题四项的算术平均（参考图行 1：(1+0.7+1+0.9951)/4 = 0.9238）。"""
    cases = [_case("a", context_recall=1.0, context_precision=0.7, faithfulness=1.0,
                   answer_relevancy=0.9951)]
    out = report.build(_run(), cases)
    assert out["cases"][0]["score"] == pytest.approx(0.9238)


def test_scores_are_rounded_to_four_places():
    cases = [_case("a", context_recall=1 / 3, context_precision=1 / 3,
                   faithfulness=1 / 3, answer_relevancy=1 / 3)]
    out = report.build(_run(), cases)
    assert out["report"]["composite_score"] == pytest.approx(0.3333)


def test_metric_order_is_fixed():
    """四张卡按参考图的顺序：召回 → 精度 → 忠实度 → 相关性。"""
    out = report.build(_run(), [_case("a", **{k: 0.9 for k in RAGAS_KEYS})])
    assert tuple(c["key"] for c in out["report"]["metrics"]) == RAGAS_KEYS


# ============================================================
# 达标线（§6.6）—— 边界值逐条验
# ============================================================

@pytest.mark.parametrize("key,threshold", sorted(THRESHOLDS.items()))
def test_threshold_boundary_is_inclusive(key, threshold):
    """卡片上写的是「达标（≥ 0.7）」—— 等于阈值算**达标**。"""
    out = report.build(_run(), [_case("a", **{k: threshold for k in RAGAS_KEYS})])
    card = next(c for c in out["report"]["metrics"] if c["key"] == key)
    assert card["threshold"] == threshold
    assert card["passed"] is True, f"{key} 恰好等于阈值时应当算达标"

    # ⚠️ 差值要**能在 4 位小数上看得见**：达标判断比的是**显示出来的那个数**
    #    （服务端已四舍五入）。用 1e-6 这种差值，分数会舍回 0.7，
    #    卡片上写着 0.7000 却标「未达标」—— 那才是真的自相矛盾。
    just_below = threshold - 0.0001
    out2 = report.build(_run(), [_case("a", **{k: just_below for k in RAGAS_KEYS})])
    card2 = next(c for c in out2["report"]["metrics"] if c["key"] == key)
    assert card2["score"] == pytest.approx(just_below)
    assert card2["passed"] is False, f"{key} 低于阈值时不能算达标"


def test_thresholds_match_the_design():
    out = report.build(_run(), [_case("a", **{k: 0.5 for k in RAGAS_KEYS})])
    got = {c["key"]: c["threshold"] for c in out["report"]["metrics"]}
    assert got == THRESHOLDS


# ============================================================
# 缺项（决策 13）
# ============================================================

def test_composite_is_null_when_any_card_is_missing():
    """缺一项 → 综合得分 `null`，**不是**剩下三项的平均。"""
    cases = [_case("a", context_recall=0.9, context_precision=0.9, faithfulness=0.9)]
    out = report.build(_run(), cases)
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["answer_relevancy"]["score"] is None
    assert out["report"]["composite_score"] is None
    # 缺的那项要说明白
    assert "答案相关性" in out["report"]["verdict"]


def test_card_is_null_when_no_case_scored_it():
    """某张卡一项都没算出来 → 「—」，**不显示 0**。"""
    cases = [_case("a", context_recall=0.9)]
    out = report.build(_run(), cases)
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["context_recall"]["score"] == pytest.approx(0.9)
    for key in ("context_precision", "faithfulness", "answer_relevancy"):
        assert cards[key]["score"] is None


def test_per_case_score_is_null_when_any_of_its_four_is_missing():
    """逐题：只要该题有**任一项**缺失 → 该项与该题的均分都显示「—」。

    不用剩下的三项去凑一个均分 —— 那看起来像个结论。
    """
    cases = [_case("a", context_recall=0.9, context_precision=0.9, faithfulness=0.9)]
    out = report.build(_run(), cases)
    row = out["cases"][0]
    assert row["context_recall"] == pytest.approx(0.9)
    assert row["answer_relevancy"] is None
    assert row["score"] is None, "缺项的题不该用三项凑均分"


def test_all_zero_is_not_missing():
    """⚠️ 四项都拿到了分、且真的都是 0 —— **那是 0，不是缺项**（§7.3 末尾）。

    参考图第 3 行就是这种：它有标准答案「换货」，只是检索没找到。
    """
    cases = [_case("a", context_recall=0.0, context_precision=0.0, faithfulness=0.0,
                   answer_relevancy=0.385)]
    out = report.build(_run(), cases)
    assert out["report"]["composite_score"] == pytest.approx(round(0.385 / 4, 4))
    assert out["cases"][0]["score"] == pytest.approx(round(0.385 / 4, 4))
    assert all(c["score"] == 0.0 or c["key"] == "answer_relevancy"
               for c in out["report"]["metrics"])


# ============================================================
# 键名拍平（§7.1）
# ============================================================

def test_keys_are_flattened_and_unprefixed():
    """库里的逐题键是 `ragas_context_recall`、整轮的是嵌套 `ragas.context_recall`
    —— 两处不一致是历史遗留。报告接口**统一吐成不带前缀、不嵌套**的形状。"""
    out = report.build(_run(), [_case("a", **{k: 1.0 for k in RAGAS_KEYS})])
    for key in RAGAS_KEYS:
        assert key in out["cases"][0]
        assert f"ragas_{key}" not in out["cases"][0]
        assert "ragas" not in out["cases"][0]
    assert all(k in out["report"] for k in
               ("composite_score", "metrics", "verdict", "ragas_available", "ragas_errors"))


# ============================================================
# 诊断字段与失败原因（§7.1 / §7.4）
# ============================================================

def test_ragas_available_and_errors_ride_in_the_report():
    """没有这两个字段，报告页四张卡全「—」时前端**无从解释**，那就是哑谜。"""
    out = report.build(_run(available=False, errors=["隔离环境不存在"]), [])
    assert out["report"]["ragas_available"] is False
    assert out["report"]["ragas_errors"] == ["隔离环境不存在"]
    assert "隔离环境不存在" in out["report"]["verdict"]


def test_error_column_is_only_filled_for_real_failures():
    """拒答**不算失败** —— 系统正确地说了不知道是个正常结果（决策 20）。"""
    cases = [
        _case("a", answer="", context_recall=0.0, context_precision=0.0,
              faithfulness=0.0, answer_relevancy=0.385),
        _case("b", error="RuntimeError: 检索炸了", answer=""),
    ]
    out = report.build(_run(), cases)
    assert out["cases"][0]["error"] is None
    assert out["cases"][1]["error"] == "RuntimeError: 检索炸了"


# ============================================================
# 结论句（§7.5）
# ============================================================

def test_verdict_when_everything_passes():
    out = report.build(_run(), [_case("a", **{k: 1.0 for k in RAGAS_KEYS})])
    assert out["report"]["verdict"] == "四项指标都在合理区间。"


def test_verdict_names_the_ones_that_missed():
    cases = [_case("a", context_recall=0.5, context_precision=0.9,
                   faithfulness=0.9, answer_relevancy=0.9)]
    verdict = report.build(_run(), cases)["report"]["verdict"]
    assert "上下文召回" in verdict
    assert "上下文精度" not in verdict
    assert "0.2" in verdict or "0.2000" in verdict      # 差多少要说出来


def test_verdict_says_why_when_it_cannot_judge():
    verdict = report.build(_run(), [])["report"]["verdict"]
    assert "上下文召回" in verdict and "缺少" in verdict
