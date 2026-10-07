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


async def score_samples(
    samples: list[dict],
    metrics: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """算一批样本的 ragas 四指标。

    `samples` 每项：`{user_input, response, retrieved_contexts, reference}`。
    返回 `{available, ok, rows, means, errors, ms}`；任何失败都**不抛**，
    而是把原因放进 `errors` —— 评测要能区分「指标算不出来」与「链路跑挂了」。
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
        return {"available": True, "ok": False, "rows": [], "means": {},
                "errors": [f"ragas 计算超时（>{timeout:.0f}s）"], "ms": 0}
    except Exception as e:  # noqa: BLE001
        return {"available": True, "ok": False, "rows": [], "means": {},
                "errors": [f"ragas 子进程启动失败: {e}"], "ms": 0}

    if err:
        # ragas/tqdm 的进度与告警走 stderr，记下来但不当成失败
        logger.info("ragas stderr: %s", err.decode("utf-8", "replace")[-800:],
                    extra={"event": "ragas.stderr"})

    text = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"available": True, "ok": False, "rows": [], "means": {},
                "errors": [f"ragas 退出码 {proc.returncode}，输出不是 JSON: {text[:300]}"],
                "ms": 0}

    data["available"] = True
    return data


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
