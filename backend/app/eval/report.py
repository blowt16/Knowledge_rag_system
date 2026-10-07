"""评测报告组装（§7）。

**服务端算，不由前端拼** —— 与 `config_label` 由服务端派生同一个理由：
指标会演进，前端拼公式就会出现「同一份数据算出两个分」。

## 算式（从参考图反推，已逐项对上）

| 量 | 算式 | 验算 |
|---|---|---|
| 四张卡 | 该项在**所有算出了分的题**上的算术平均 | 召回列 (1+1+0+1+1)/5 = 0.8 ✓ |
| **综合得分** | 四张卡的算术平均 | (0.8+0.74+0.8+0.855)/4 = 0.79875 → 0.7988 ✓ |
| 逐题**均分** | 该题四项的算术平均 | 行1 (1+0.7+1+0.9951)/4 = 0.9238 ✓ |

## 缺项就不给数（决策 13）

拿不齐的数凑平均值，比不给数更误导 —— 它看起来像个结论。
所以：某张卡一项都没算出来 → 显示「—」；**四项齐全才出综合得分**；
逐题只要有任一项缺失 → 该项与该题的均分都显示「—」。

⚠️ 别和「四项都拿到了分、且真的都是 0」混为一谈（§7.3 末尾）：参考图第 3 行
就是那种 —— 它有标准答案「换货」，只是检索没找到，**所以那个 0 是真的**。

## 键名拍平

库里逐题的键是带前缀的 `metrics.ragas_context_recall`，整轮的键是嵌套的
`metrics.ragas.context_recall`（**两处不一致，是历史遗留**）。报告接口统一吐成
**不带前缀、不嵌套**的 `context_recall`，前端只认这一种形状 ——
拼接在这里做，别让前端去猜该读哪个键。
"""

from __future__ import annotations

import json
from typing import Any

#: 四项的**固定顺序**（照参考图：召回 → 精度 → 忠实度 → 相关性）。
#: 服务端定序，前端不排 —— 不然表头顺序会随数据乱跳。
METRIC_KEYS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")

#: 达标线（参考图卡片底下那行小字）。**阈值与达标判断放服务端** ——
#: 那是判断，不是文案；文案（中文名/提示语）在前端。
THRESHOLDS: dict[str, float] = {
    "context_recall": 0.7,
    "context_precision": 0.6,
    "faithfulness": 0.8,
    "answer_relevancy": 0.7,
}

#: 结论句里要念出指标的中文名。前端自己也有一份（卡片标题用）——
#: 这是方案 §7.1 定的分工（「中文名放前端」），改词要**两边一起改**。
LABELS: dict[str, str] = {
    "context_recall": "上下文召回",
    "context_precision": "上下文精度",
    "faithfulness": "忠实度",
    "answer_relevancy": "答案相关性",
}


def _loads(value: Any) -> dict:
    """asyncpg 把 JSONB 读成 str。"""
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except ValueError:
            return {}
    return value or {}


def _number(value: Any) -> float | None:
    """只有真正的数字才算「算出了分」—— 布尔是 int 的子类，要挡掉。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _case_values(case: dict) -> dict[str, float | None]:
    """从**带前缀**的逐题 metrics 里取四项（`ragas_context_recall` 这种）。"""
    metrics = _loads(case.get("metrics"))
    return {key: _number(metrics.get(f"ragas_{key}")) for key in METRIC_KEYS}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def build(run_row: dict, case_rows: list[dict]) -> dict:
    """组装 `GET /runs/{id}` 的 `report` 块与逐题明细。"""
    metrics = _loads(run_row.get("metrics")) if run_row.get("metrics") else {}
    available = metrics.get("ragas_available")
    errors = list(metrics.get("ragas_errors") or [])

    rows = [{"case_id": c.get("case_id"),
             "question": c.get("question"),
             "ground_truth": c.get("ground_truth"),
             "answer": c.get("answer") or "",
             # ⚠️ **只有真跑挂了才填**（决策 20）。拒答不算失败 ——
             #    「系统正确地说了不知道」是个正常结果，而且「生成的答案」那列
             #    已经把原因写着了。所以这一列大多数时候是空的，这是对的。
             "error": c.get("error"),
             **_case_values(c)}
            for c in case_rows]

    for row in rows:
        four = [row[key] for key in METRIC_KEYS]
        # 缺任一项 → 该题均分「—」，不用剩下三项去凑
        row["score"] = _mean([v for v in four if v is not None]) \
            if all(v is not None for v in four) else None

    cards = []
    for key in METRIC_KEYS:
        scores = [r[key] for r in rows if r[key] is not None]
        score = _mean(scores)
        cards.append({
            "key": key,
            "score": score,
            "threshold": THRESHOLDS[key],
            # 缺项时 `passed` 只能是 False —— 但它没有「没达标」的意思，
            # 前端在 `score is None` 时显示「—」而不是红色的「未达标」。
            "passed": score is not None and score >= THRESHOLDS[key],
        })

    # **四项齐全才出综合得分**；缺任何一项显示「—」并说明缺哪项
    composite = _mean([c["score"] for c in cards]) \
        if all(c["score"] is not None for c in cards) else None

    return {
        "report": {
            "composite_score": composite,
            "metrics": cards,
            "verdict": _verdict(cards, available, errors),
            # ⚠️ 这两个**必须在 report 里**，不能只在 metrics 里：报告页四张卡全「—」时
            #    前端得能说清为什么（环境不在 / 有指标没算出来 / 没有可评分样本）。
            #    没有它们，那就是哑谜。
            "ragas_available": available,
            "ragas_errors": errors,
        },
        "cases": rows,
    }


def _verdict(cards: list[dict], available: Any, errors: list[str]) -> str:
    """结论句（§7.5）。

    大多数时候这一页只有一句话可读，所以它得把「能不能下结论」说清楚，
    而不是含糊地说一句「见上表」。
    """
    missing = [c["key"] for c in cards if c["score"] is None]
    if missing:
        names = "、".join(LABELS[k] for k in missing)
        if available is False:
            why = f"（{errors[0]}）" if errors else ""
            return f"{names}没算出来：ragas 隔离环境不可用{why}，见 docs/评测与ragas.md。"
        if errors:
            return f"{names}没算出来：{errors[0]}"
        return (f"缺少{names} —— 这几项没有可评分的样本（缺标准答案或检索上下文），"
                "综合得分先不给。")

    failed = [c for c in cards if not c["passed"]]
    if not failed:
        return "四项指标都在合理区间。"
    parts = [f"{LABELS[c['key']]} {c['score']:.4f}"
             f"（差 {c['threshold'] - c['score']:.4f} 到 {c['threshold']}）"
             for c in failed]
    return "有未达标：" + "；".join(parts) + "。"


def duration_ms(run_row: dict) -> int | None:
    """耗时(ms) = `finished_at - started_at`（**不存列**，现算）。

    少一个要保持同步的字段：存了就得保证每个改状态的路径都顺手更新它，
    而现算不可能算错。
    """
    started, finished = run_row.get("started_at"), run_row.get("finished_at")
    if not started or not finished:
        return None
    return int((finished - started).total_seconds() * 1000)
