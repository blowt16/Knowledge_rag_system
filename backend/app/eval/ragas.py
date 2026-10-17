"""ragas 指标计算的服务端入口（主环境侧）。

**为什么是子进程而不是 import**：ragas 的依赖树不能进主环境 ——
装进来会让 `import sentence_transformers` 段错误（reranker 直接加载不了），
且 ragas 0.4.3 本身与 langchain-community 0.4.x 不兼容（`vertexai` 模块已删）。
完整实测记录见 `backend/tools/ragas_runner.py` 模块头与 `docs/评测与ragas.md`。

所以这里只做三件事：**找到隔离环境的解释器 → 把样本喂进去 → 把分数收回来**。
计算本身在 `.venv-ragas/Scripts/python.exe backend/tools/ragas_runner.py` 里。

⚠️ 隔离环境不存在时**不抛异常**，返回 `available: False` —— 与 `stats/retrieval`
   对 Prometheus 的处理同一口径（M4-D5）：依赖不可用不该让别人 500。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from app.core.config import repo_path

logger = logging.getLogger(__name__)

METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

#: 评测跑一轮要调 LLM 判官，60–80 题是分钟级 —— 超时给足
DEFAULT_TIMEOUT = 3600.0


def venv_python() -> Path | None:
    """隔离环境的解释器路径。Windows 是 Scripts/，POSIX 是 bin/。"""
    override = os.environ.get("RAGAS_PYTHON")
    if override:
        p = Path(override)
        return p if p.exists() else None
    for rel in ((".venv-ragas", "Scripts", "python.exe"),
                (".venv-ragas", "bin", "python")):
        candidate = repo_path(*rel)
        if candidate.exists():
            return candidate
    return None


def runner_script() -> Path:
    return repo_path("backend", "tools", "ragas_runner.py")


def available() -> bool:
    return venv_python() is not None and runner_script().exists()


#: 指标分批：**检索侧先算、生成侧后算**，每一批是**一个独立子进程、各带超时**。
#:
#: 为什么拆（2026-10-17 实测）：四项原来在一次 `evaluate()` 里算，
#: **任何一项卡住或抛错，四项一起丢**。那轮 Jina 的评测里 RAGAS 子进程
#: CPU 恒为 0、12 分钟无进展、最终 stdout 0 字节 —— 唯一兜底是整轮 3600s 超时，
#: 等于一小时白等，而且忠实度/召回这些**已经算得出来**的也被一起扔掉。
#:
#: 拆批之后：
#:   - 一批卡死被 kill，**另一批的结果照样落库**
#:   - 失败原因能说清是「检索侧没算出来」还是「生成侧没算出来」
#:   - 每批单独计时，慢在哪一侧一目了然
#:
#: ⚠️ 批内顺序有意义：检索侧只用「问题/上下文/参考答案」，**不依赖生成质量**；
#:    生成侧要看答案。先算检索侧 = 即使生成侧崩了，检索质量仍然可测。
METRIC_BATCHES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("retrieval", ("context_precision", "context_recall")),
    ("generation", ("faithfulness", "answer_relevancy")),
)

#: 单批超时。整轮的 `DEFAULT_TIMEOUT`（3600s）仍作为总闸，但**单批先卡住** ——
#: 一批 10 分钟没产出就放弃它，别让另一批陪葬。
BATCH_TIMEOUT = 600.0


def batches_for(want: list[str]) -> list[tuple[str, list[str]]]:
    """把请求的指标按批次切开；**不在任何批次里的指标单独成一批，不静默丢**。"""
    out: list[tuple[str, list[str]]] = []
    covered: set[str] = set()
    for label, names in METRIC_BATCHES:
        picked = [n for n in names if n in want]
        if picked:
            out.append((label, picked))
            covered.update(picked)
    leftover = [n for n in want if n not in covered]
    if leftover:
        out.append(("other", leftover))
    return out


async def _run_batch(python, script, samples: list[dict], want: list[str],
                     timeout: float) -> dict:
    """跑**一批**指标（一个子进程）。任何失败都返回 `ok=False` + 原因，不抛。"""
    payload = json.dumps({"samples": samples, "metrics": want},
                         ensure_ascii=False).encode("utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            str(python), str(script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(script.parents[2]),
        )
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except TimeoutError:
        return {"ok": False, "rows": [], "means": {},
                "errors": [f"ragas 计算超时（>{timeout:.0f}s）"], "ms": 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "rows": [], "means": {},
                "errors": [f"ragas 子进程启动失败: {e}"], "ms": 0}

    if err:
        # ragas/tqdm 的进度与告警走 stderr，记下来但不当成失败
        logger.info("ragas stderr: %s", err.decode("utf-8", "replace")[-800:],
                    extra={"event": "ragas.stderr"})

    text = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # ⚠️ **诊断必须给「解析器报的错」和「输出的末尾」**，不能只给开头。
        #    实测踩过：线上两次报「输出不是 JSON: {"ok": true, …」——存的是 `text[:300]`，
        #    而开头**明明是合法 JSON**（结果其实算出来了），真正出问题的地方在
        #    300 字符之后。按这条信息查了两天，同样的调用与输入一次都复现不出来。
        #    报错文本会进 `ragas_errors` → 报告页，所以两头都截断，别失控。
        head = text[:120]
        tail = text[-260:] if len(text) > 380 else ""
        detail = f"头部 {head!r}" + (f"；末尾 {tail!r}" if tail else "")
        return {"ok": False, "rows": [], "means": {},
                "errors": [f"ragas 输出不是 JSON：{e}（stdout 共 {len(out)} 字节；{detail}）"],
                "ms": 0}

    return data


async def score_samples(
    samples: list[dict],
    metrics: list[str] | None = None,
    timeout: float = BATCH_TIMEOUT,
) -> dict:
    """分批算 ragas 指标（检索侧一批、生成侧一批）。

    `samples` 每项：`{user_input, response, retrieved_contexts, reference}`。
    返回 `{available, ok, rows, means, errors, ms}`；任何失败都**不抛**，
    而是把原因放进 `errors` —— 评测要能区分「指标算不出来」与「链路跑挂了」。

    ⚠️ `timeout` 现在是**单批**超时（默认 600s），不是整轮。
       两批串行跑，整轮最坏 ≈ 2×timeout。
    """
    want = list(metrics or METRIC_NAMES)
    if not samples:
        return {"available": True, "ok": True, "rows": [], "means": {},
                "errors": [], "ms": 0}

    python = venv_python()
    script = runner_script()
    if python is None or not script.exists():
        logger.warning("ragas 隔离环境不可用，跳过指标计算",
                       extra={"event": "ragas.unavailable",
                              "python": str(python), "script": str(script)})
        return {"available": False, "ok": False, "rows": [], "means": {},
                "errors": ["ragas 隔离环境不存在（.venv-ragas）；"
                           "见 docs/评测与ragas.md 的建环境命令"], "ms": 0}

    merged_rows: list[dict] = [{} for _ in samples]
    means: dict = {}
    errors: list[str] = []
    total_ms = 0
    ok_any = False

    for label, batch in batches_for(want):
        result = await _run_batch(python, script, samples, batch, timeout)
        total_ms += int(result.get("ms") or 0)

        if result.get("ok"):
            ok_any = True
        else:
            # ⚠️ 失败原因必须带批次名：报告页只看到一句「ragas 出错」时，
            #    分不清是检索侧还是生成侧，等于没诊断。
            reason = "；".join(result.get("errors") or ["未知失败"])
            errors.append(f"[{label}] {reason}")

        for i, row in enumerate(result.get("rows") or []):
            if i < len(merged_rows):
                merged_rows[i].update(row)
        means.update(result.get("means") or {})

    # 一批都没成、且没有任何一项算出分数 → 保持既有契约：rows 为空
    if not ok_any and not any(merged_rows):
        return {"available": True, "ok": False, "rows": [], "means": {},
                "errors": errors, "ms": total_ms}

    return {"available": True, "ok": True, "rows": merged_rows,
            "means": means, "errors": errors, "ms": total_ms}


if __name__ == "__main__":  # 手工冒烟：python -m app.eval.ragas
    async def _demo() -> None:
        r = await score_samples([{
            "user_input": "缓考申请需要什么条件？",
            "response": "学生因病因事不能参加考试的，应当在考试前向所在学院提出缓考申请。",
            "retrieved_contexts": ["学生因病因事不能参加考试的，应当在考试前向所在学院提出申请，经批准后方可缓考。"],
            "reference": "学生因病因事不能参加考试的，可在考前向所在学院申请缓考。",
        }])
        print(json.dumps(r, ensure_ascii=False, indent=2))

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(_demo())
