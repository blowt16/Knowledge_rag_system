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


def _run(*, means=None, available=True, errors=None,
         scored=None, cases=None) -> dict:
    metrics = {"ragas": means or {}, "ragas_available": available,
               "ragas_errors": errors or []}
    if scored is not None:
        metrics["ragas_scored_cases"] = scored
    if cases is not None:
        metrics["cases"] = cases
    return {"metrics": metrics, "status": "done", "error": None,
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
    # 老轮次（库里没有 `ragas_scored_cases`）照旧给原句 —— 报告是历史数据的视图
    out = report.build(_run(), [_case("a", **{k: 1.0 for k in RAGAS_KEYS})])
    assert out["report"]["verdict"] == "四项指标都在合理区间。"


# ============================================================
# RAGAS 的分母（2026-10-16 口径修正）
# ============================================================
# ⚠️ 四项均值现在只算在**作答的题**上（拒答不送 ragas，见 `eval/runner.py` 的门槛）。
#    分母变了就必须说出来，否则 6 题算出来的均值会被当成 10 题的读 ——
#    实测那轮：answer_relevancy 10 题 0.5494、6 道作答题 0.9157。

def test_report_carries_the_ragas_denominator():
    """整轮汇总里的 `ragas_scored_cases` 要原样进报告 —— 前端拿它和用例数并排显示。"""
    out = report.build(_run(means={k: 0.9 for k in RAGAS_KEYS}, scored=6, cases=10),
                       [_case("a", **{k: 0.9 for k in RAGAS_KEYS})])
    assert out["report"]["ragas_scored_cases"] == 6


def test_denominator_is_absent_for_old_runs():
    """老轮次没有这个字段 → `None`，**不是 0**（0 会被读成「一道都没算」）。
    结论句也照旧不念分母 —— 当时的口径是另一套（拒答也送了 ragas）。
    卡片上的 `scored_cases` 是**现算的**（描述的就是卡片的均值本身），不受影响。
    """
    out = report.build(_run(), [_case("a", **{k: 0.9 for k in RAGAS_KEYS})])
    assert out["report"]["ragas_scored_cases"] is None
    assert "题有分" not in out["report"]["verdict"]


def test_verdict_names_the_denominator_when_everything_passes():
    """结论句要把「几分之几」念出来 —— 这是报告页唯一一句话，漏了就没人知道分母。

    ⚠️ 分母念的是**四张卡各自的非 None 计数**（不是整轮 `ragas_scored_cases`）：
       两者在正常轮次上相等；单题判官失败时卡片的更小，句子必须念小的那个
       （见下一条的「各指标 2–3」）。
    """
    cases = [_case(f"c{i}", **{k: 0.9 for k in RAGAS_KEYS}) for i in range(6)]
    verdict = report.build(
        _run(means={k: 0.9 for k in RAGAS_KEYS}, scored=6, cases=10),
        cases)["report"]["verdict"]
    assert verdict == "四项指标都在合理区间（6/10 题有分）。"


def test_verdict_names_the_denominator_when_something_missed():
    cases = [_case(f"c{i}", context_recall=0.5, context_precision=0.9,
                   faithfulness=0.9, answer_relevancy=0.9) for i in range(3)]
    verdict = report.build(_run(scored=3, cases=4), cases)["report"]["verdict"]
    assert verdict == "有未达标（3/4 题有分）：上下文召回 0.5000（差 0.2000 到 0.7）。"


def test_card_carries_its_own_denominator():
    """★ 每张卡带上「这张卡的均值算在几道题上」。

    与整轮 `ragas_scored_cases`（本轮有几道题拿到了分）**不是一回事**：
    单题判官失败时那一行保留、值全 None（`tools/ragas_runner.py` 的 NaN→None），
    各指标的有分题数可以不同 —— 评审实测 506f109f：召回/精度/忠实度 42、相关性 43，
    而整轮计数是 44。均值旁边必须是它自己的分母。
    """
    cases = [
        _case("a", context_recall=0.9, context_precision=0.9, faithfulness=0.9,
              answer_relevancy=0.9),
        _case("b", context_recall=0.9, context_precision=0.9, faithfulness=0.9,
              answer_relevancy=0.9),
        _case("c", context_recall=0.9, context_precision=0.9, answer_relevancy=0.9),
    ]
    out = report.build(_run(means={k: 0.9 for k in RAGAS_KEYS}, scored=3, cases=3), cases)
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["context_recall"]["scored_cases"] == 3
    assert cards["faithfulness"]["scored_cases"] == 2, \
        "c 题没算出忠实度 —— 这张卡的分母是 2，不能拿 3 盖上去"


def test_verdict_names_the_range_when_metrics_scored_differently():
    """各指标的有分题数不同（判官偶发单题失败）→ 结论句念**范围**：
    决不把大样本的数字盖在小样本的均值上（这正是 63 那类错的最小残留版）。"""
    cases = [
        _case("a", context_recall=0.9, context_precision=0.9, faithfulness=0.9,
              answer_relevancy=0.9),
        _case("b", context_recall=0.9, context_precision=0.9, faithfulness=0.9,
              answer_relevancy=0.9),
        _case("c", context_recall=0.9, context_precision=0.9, answer_relevancy=0.9),
    ]
    verdict = report.build(_run(means={k: 0.9 for k in RAGAS_KEYS}, scored=3, cases=4),
                           cases)["report"]["verdict"]
    assert verdict == "四项指标都在合理区间（各指标 2–3/4 题有分）。"


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


def test_verdict_mentions_refusal_as_a_way_to_get_no_samples():
    """拒答**不进 ragas** 之后（见 `eval/runner.py` 的门槛），「四项都没分」
    多了一种正常原因：整轮都拒答了。不写出来的话，用户只会以为环境坏了。"""
    verdict = report.build(_run(scored=0, cases=4), [])["report"]["verdict"]
    assert "拒答" in verdict


def test_verdict_never_embeds_a_raw_diagnostic_dump():
    """⚠️ 结论句里**不能整段照抄后端输出**（评审后实测发现的那面「JSON 墙」）。

    库里存着一条真实的历史错误，`ragas_errors[0]` 是一整串 349 字的输出
    （`ragas 退出码 0，输出不是 JSON: {"ok": true, ...}`）。原样拼进结论句之后，
    报告弹窗顶上会出现三行密密麻麻的 JSON —— 用户从中读不到任何能行动的信息，
    而排版被彻底撑乱。

    全文仍然留在 `report.ragas_errors` 里（接口可见、悬停可见），
    只是**结论句**这一行要短。
    """
    long_err = 'ragas 退出码 0，输出不是 JSON: ' + '{"ok": true, "rows": [' + 'x' * 340
    out = report.build(_run(available=True, errors=[long_err]), [])
    verdict = out["report"]["verdict"]
    assert "上下文召回" in verdict, "还得说清是哪几项没算出来"
    assert "见" in verdict or "日志" in verdict, "要指个去处，不能只说一句空话"
    assert len(verdict) < 120, f"结论句被后端输出撑爆了：{len(verdict)} 字"
    # ⚠️ 全文不能丢 —— 要排查的人从接口/悬停里照样拿得到
    assert out["report"]["ragas_errors"] == [long_err]


def test_short_errors_are_still_shown_verbatim():
    """短错误照原样显示 —— 不能一律截断，那样就查不出东西了。"""
    out = report.build(_run(available=False, errors=["ragas 隔离环境不存在"]), [])
    assert "ragas 隔离环境不存在" in out["report"]["verdict"]


# ============================================================
# 多轮：按轮聚合（§6.5）
# ============================================================

def _turn(idx, **ragas):
    return {"turn": idx, "question": f"q{idx}",
            **{f"ragas_{k}": v for k, v in ragas.items()}}


def _multi_run(*, scored_turns=None, turns_total=4, turns_with_reference=2):
    metrics = {"cases": 1, "ragas_available": True, "ragas_errors": [],
               "multi_turn": {"turns_total": turns_total,
                              "turns_completed": turns_total,
                              "turns_with_reference": turns_with_reference},
               "ragas_scored_turns": scored_turns}
    return {"metrics": metrics, "dialogue_mode": "multi"}


def _multi_case(turns):
    return {"case_id": "c1", "question": "q", "ground_truth": None, "answer": "a",
            "error": None, "metrics": {"turns": turns}}


def test_multi_mode_cards_average_per_turn():
    case = _multi_case([
        _turn(1, faithfulness=1.0, answer_relevancy=0.8,
              context_precision=None, context_recall=None),
        _turn(2, faithfulness=0.5, answer_relevancy=0.6,
              context_precision=None, context_recall=None),
    ])
    out = report.build(_multi_run(scored_turns=2), [case])
    cards = {c["key"]: c for c in out["report"]["metrics"]}
    assert cards["faithfulness"]["score"] == 0.75
    assert cards["faithfulness"]["scored_samples"] == 2
    assert cards["faithfulness"]["sample_unit"] == "turn"
    assert cards["context_recall"]["score"] is None
    assert out["report"]["dialogue_mode"] == "multi"
    assert out["report"]["multi_turn"]["turns_total"] == 4
    assert out["report"]["ragas_scored_turns"] == 2


def test_multi_mode_per_case_row_uses_non_null_turn_mean():
    case = _multi_case([
        _turn(1, faithfulness=1.0, answer_relevancy=0.8,
              context_precision=None, context_recall=None),
        _turn(2, faithfulness=0.5, answer_relevancy=0.6,
              context_precision=None, context_recall=None),
    ])
    out = report.build(_multi_run(), [case])
    row = out["cases"][0]
    assert row["faithfulness"] == 0.75
    assert row["answer_relevancy"] == 0.7
    assert row["context_precision"] is None
    assert row["score"] is None
    assert len(row["turns"]) == 2


def test_multi_verdict_counts_turns():
    case = _multi_case([
        _turn(1, faithfulness=0.9, answer_relevancy=0.9,
              context_precision=0.9, context_recall=0.9),
    ])
    out = report.build(
        _multi_run(scored_turns=1, turns_total=3, turns_with_reference=1), [case])
    assert "轮有分" in out["report"]["verdict"]
    assert "题有分" not in out["report"]["verdict"]


def test_multi_verdict_explains_missing_context_metrics():
    case = _multi_case([
        _turn(1, faithfulness=0.9, answer_relevancy=0.9),
    ])
    verdict = report.build(
        _multi_run(scored_turns=1, turns_total=3, turns_with_reference=0),
        [case])["report"]["verdict"]
    assert "逐轮标准答案" in verdict
