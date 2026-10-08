"""ragas 指标计算入口 —— **必须用 `.venv-ragas` 里的解释器跑**，不要用主环境。

    .venv-ragas/Scripts/python.exe backend/tools/ragas_runner.py < payload.json  > out.json

为什么单独一个 venv（2026-10-06 实测，别推翻）：

1. **ragas 0.4.3 与 langchain-community 0.4.x 不兼容** —— 它 `import` 时就要
   `langchain_community.chat_models.vertexai`，而那个模块在 0.4.x 已被删除。
   → 隔离环境里钉 `langchain-community<0.4` 才 import 得动。
2. **把 ragas 装进主环境会让 `import sentence_transformers` 段错误**（Windows
   access violation，崩在 `pyarrow/__init__.py` 初始化），而 reranker 正是靠
   sentence-transformers 加载的。受控实验：装 ragas → 段错误；回滚 → 恢复正常。
   根因未定位到具体是哪个传递依赖，但结论是硬的：**ragas 的依赖树不能进主环境**。

协议：stdin 收 JSON、stdout 出 JSON（**stdout 只允许出现结果 JSON**，其它一律 stderr）。

    入参 {"samples": [{"user_input","response","retrieved_contexts","reference"}],
          "metrics": ["faithfulness","answer_relevancy","context_precision","context_recall"]}
    出参 {"ok": true, "ms": 1234, "rows": [{...每条的四个分数...}],
          "means": {...}, "errors": [...]}

⚠️ **judge 必须关思考**（与 §1.0 同一口径）：deepseek-flash 默认思考模式下会把
   整个 max_tokens 预算烧在 `reasoning_content` 上、`content` 为空 —— 实测
   finish_reason=length、reasoning 1818 字、content 0 字，ragas 随即报
   `IncompleteOutputException`。关思考后 finish=stop、content 12 字，正常。
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REPO = Path(__file__).resolve().parents[2]

METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = REPO / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


#: **真正的** stdout。
#:
#: ⚠️ 运行时它会被换成「开局抢下来的那个 stdout」，而 `sys.stdout` 会被改成 stderr ——
#:    见 `__main__` 里的说明。所有**结果**都必须走 `_emit()`，不要直接写 `sys.stdout`。
_real_stdout = sys.stdout


def _emit(payload: dict) -> None:
    """把结果写到真 stdout。**只有它和 `_fail()` 能碰真 stdout。**"""
    json.dump(payload, _real_stdout, ensure_ascii=False)
    _real_stdout.flush()


def _fail(msg: str) -> None:
    _emit({"ok": False, "error": msg})


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:  # noqa: BLE001
        _fail(f"入参不是合法 JSON: {e}")
        return 2

    samples = payload.get("samples") or []
    wanted = payload.get("metrics") or list(METRIC_NAMES)
    unknown = [m for m in wanted if m not in METRIC_NAMES]
    if unknown:
        _fail(f"未知指标 {unknown}，可选 {list(METRIC_NAMES)}")
        return 2
    if not samples:
        _emit({"ok": True, "ms": 0, "rows": [], "means": {}, "errors": []})
        return 0

    env = _load_env()
    from openai import OpenAI

    judge = OpenAI(api_key=env.get("DEEPSEEK_API_KEY"),
                   base_url=env.get("DEEPSEEK_BASE_URL"))
    embedder = OpenAI(api_key=env.get("ALIYUN_ACCESS_KEY"),
                      base_url=env.get("ALIYUN_BASE_URL"))

    from langchain_openai import OpenAIEmbeddings as LcEmbeddings
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import llm_factory
    from ragas.metrics import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    llm = llm_factory(
        "deepseek-flash",
        client=judge,
        reasoning_effort="none",   # ★ 见模块头：不关思考则 content 为空
        max_tokens=4096,
    )
    # ⚠️ 用 langchain 式嵌入：ragas 0.4.3 的 `ragas.embeddings.OpenAIEmbeddings`
    #    只有 `embed_text`，而 AnswerRelevancy 要 `embed_query` → AttributeError。
    emb = LcEmbeddings(
        model="qwen3.7-text-embedding",
        api_key=env.get("ALIYUN_ACCESS_KEY"),
        base_url=env.get("ALIYUN_BASE_URL"),
        check_embedding_ctx_length=False,
    )

    available = {
        "faithfulness": Faithfulness,
        "answer_relevancy": AnswerRelevancy,
        "context_precision": ContextPrecision,
        "context_recall": ContextRecall,
    }

    ragas_samples = [
        SingleTurnSample(
            user_input=s.get("user_input", ""),
            response=s.get("response", ""),
            retrieved_contexts=list(s.get("retrieved_contexts") or []),
            reference=s.get("reference") or None,
        )
        for s in samples
    ]

    t0 = time.time()
    result = evaluate(
        EvaluationDataset(samples=ragas_samples),
        metrics=[available[m]() for m in wanted],
        llm=llm,
        embeddings=emb,
        raise_exceptions=False,     # 单题失败不该拖垮整轮
        show_progress=False,
        # ⚠️ **必须设**：不设时 ragas 逐题串行，每题约 40 秒 ——
        #    消融 8 行 × 63 题会变成 5 小时量级（实测第 1 行跑了 25 分钟还没完）。
        #    设成 8 之后同一批并发算，整轮从"小时"降到"分钟"。
        #    不要调太大：每题要调若干次 LLM 判官，并发高了会被上游限流。
        batch_size=8,
    )
    ms = int((time.time() - t0) * 1000)

    df = result.to_pandas()
    rows: list[dict] = []
    errors: list[str] = []
    for idx in range(len(df)):
        row: dict = {}
        for m in wanted:
            if m not in df.columns:
                continue
            v = df.iloc[idx][m]
            try:
                fv = float(v)
                row[m] = None if fv != fv else round(fv, 4)   # NaN != NaN
            except (TypeError, ValueError):
                row[m] = None
        rows.append(row)

    means: dict = {}
    for m in wanted:
        vals = [r[m] for r in rows if r.get(m) is not None]
        means[m] = round(sum(vals) / len(vals), 4) if vals else None
        if not vals:
            errors.append(f"{m}: 全部为 None（该指标本次没有算出来）")

    _emit({"ok": True, "ms": ms, "rows": rows, "means": means, "errors": errors})
    return 0


if __name__ == "__main__":
    # ⚠️ 模块头写着「stdout 只允许出现结果 JSON，其它一律 stderr」——
    #    但在这次改动之前**没有任何东西保证它**：ragas / tqdm / langchain / torch
    #    任何一个往 stdout 写点东西（进度条、告警、调试输出），都会并进结果里，
    #    整段就解析不出来了。线上真出过两次「退出码 0、输出不是 JSON」，
    #    而输出开头明明是合法的（分数其实算出来了），排查时无从下手。
    #
    #    做法：把真 stdout 抢到 `_real_stdout`，然后把 `sys.stdout` **换成 stderr**。
    #    之后第三方库爱往 stdout 写什么都行 —— 它们写的东西会落到 stderr，
    #    而结果 JSON 走 `_emit()` 直接写真 stdout，两者再也不会打架。
    # （这里在模块层，直接赋值就是改模块全局 —— 加了 `global` 反而会因为
    #   「先赋值后声明 global」直接 SyntaxError，整个 runner 都跑不起来）
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        _real_stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 老解释器没有 reconfigure
        pass
    sys.exit(main())
