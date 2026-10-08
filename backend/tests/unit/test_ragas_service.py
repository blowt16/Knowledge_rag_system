"""ragas 服务的护栏（不联网、不起真子进程）。

真跑 ragas 要调 LLM 判官（一条样本约 20 秒），不适合放进快速测试套件 ——
**真链路的手工验证方式见 `docs/评测与ragas.md`**：
    cd backend && uv run python -m app.eval.ragas

这里锁的是三件事：
1. 隔离环境不在时**不抛异常**，而是返回 `available: False`（与 stats/retrieval 同口径）
2. 空样本不浪费一次子进程
3. 解释器路径的解析规则（`RAGAS_PYTHON` 覆盖 + 平台差异）
"""

from __future__ import annotations

import pytest

from app.eval import ragas as ragas_service


def test_venv_python_honours_override(monkeypatch, tmp_path):
    """RAGAS_PYTHON 指到哪儿就用哪儿；指向不存在的路径返回 None。"""
    fake = tmp_path / "py"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("RAGAS_PYTHON", str(fake))
    assert ragas_service.venv_python() == fake

    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))
    assert ragas_service.venv_python() is None


def test_available_false_without_venv(monkeypatch, tmp_path):
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))
    assert ragas_service.available() is False


async def test_score_samples_degrades_when_venv_missing(monkeypatch, tmp_path):
    """隔离环境不存在 → 返回 available=False + 可行动的提示，**不抛异常**。"""
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))

    result = await ragas_service.score_samples([
        {"user_input": "问", "response": "答", "retrieved_contexts": ["c"]},
    ])

    assert result["available"] is False
    assert result["ok"] is False
    assert result["rows"] == []
    assert any("评测与ragas.md" in e for e in result["errors"]), \
        "错误提示要告诉人下一步去哪儿看（否则只会看到『指标算不出来』）"


async def test_score_samples_empty_short_circuits(monkeypatch, tmp_path):
    """空样本直接返回，不去起子进程（也不因缺环境而报 unavailable）。"""
    monkeypatch.setenv("RAGAS_PYTHON", str(tmp_path / "nope"))

    result = await ragas_service.score_samples([])

    assert result == {"available": True, "ok": True, "rows": [], "means": {},
                      "errors": [], "ms": 0}


def test_metric_names_match_runner():
    """服务端与 runner 的指标名必须一致 —— 不一致会让整批指标静默变 None。"""
    runner_src = ragas_service.runner_script().read_text(encoding="utf-8")
    for name in ragas_service.METRIC_NAMES:
        assert f'"{name}"' in runner_src, f"{name} 在 runner 里找不到"


@pytest.mark.parametrize("name", [
    "faithfulness", "answer_relevancy", "context_precision", "context_recall",
])
def test_metric_names_are_the_ragas_four(name):
    assert name in ragas_service.METRIC_NAMES


# ============================================================
# 解析失败时的诊断信息
# ============================================================
# ⚠️ 这一条是**真事**逼出来的：线上出现两次
#    「ragas 退出码 0，输出不是 JSON: {"ok": true, …」——错的那一轮 ragas 其实
#    **算出了分数**，而报错里存的 `text[:300]` 是输出的**开头**，开头明明是合法 JSON。
#    真正能定位问题的是**解析器报的什么错**和**输出的末尾**，这两样原来都丢掉了，
#    于是查了两天查不出原因（复现：同样的调用、同样的输入、同样 3 个样本，一次都复现不出来）。

_FAKE_RUNNER = '''
import sys
sys.stdin.read()                      # 父进程会写 stdin，读完再退出免得 BrokenPipe
# 合法 JSON 后面跟了一段进度条 —— 这正是「头合法、整段不合法」的形状
sys.stdout.write('{"ok": true, "ms": 1, "rows": [{"faithfulness": 1.0}], '
                 '"means": {"faithfulness": 1.0}, "errors": []}')
sys.stdout.write("Evaluating: 100%|##########| 3/3 [00:01<00:00, 2.1it/s]")
'''


def _fake_runner(monkeypatch, tmp_path):
    """把子进程换成一段可控输出的脚本（不起真 ragas、不联网）。"""
    import sys as _sys
    from pathlib import Path as _Path
    # parents[2] 会被当成 cwd，所以多造两层目录
    script = tmp_path / "a" / "b" / "c" / "fake_runner.py"
    script.parent.mkdir(parents=True)
    script.write_text(_FAKE_RUNNER, encoding="utf-8")
    monkeypatch.setattr(ragas_service, "venv_python", lambda: _Path(_sys.executable))
    monkeypatch.setattr(ragas_service, "runner_script", lambda: script)
    return script


async def test_parse_failure_reports_the_decoder_reason_and_the_tail(monkeypatch, tmp_path):
    _fake_runner(monkeypatch, tmp_path)
    result = await ragas_service.score_samples([
        {"user_input": "q", "response": "a", "retrieved_contexts": ["c"], "reference": "r"}])

    assert result["ok"] is False and result["rows"] == []
    err = result["errors"][0]
    # ① 解析器**原话**（「Extra data」这种）——原来一个字都没有
    assert "Extra data" in err, err
    # ② **末尾**那段 —— 出问题的正是它，原来也被丢掉（只留了开头）
    assert "Evaluating: 100%" in err, err
    # ③ 字节数：用来判断是「被截断」还是「多了东西」
    assert "字节" in err, err


async def test_parse_failure_message_stays_a_reasonable_length(monkeypatch, tmp_path):
    """诊断要够用但不能失控 —— 它会进 `ragas_errors`、进而进报告页。"""
    _fake_runner(monkeypatch, tmp_path)
    result = await ragas_service.score_samples([
        {"user_input": "q", "response": "a", "retrieved_contexts": ["c"], "reference": "r"}])
    assert len(result["errors"][0]) < 1200, len(result["errors"][0])


def test_runner_keeps_third_party_stdout_out_of_the_result():
    """⚠️ **结果 JSON 必须独占 stdout。**

    模块头写着「stdout 只允许出现结果 JSON，其它一律 stderr」，但在这次改动之前
    **没有任何东西保证它** —— ragas / tqdm / langchain / torch 任何一个往 stdout
    写东西（进度条、告警、调试输出）都会并进结果里，整段就解析不出来。
    线上真出过两次「退出码 0、输出不是 JSON，而开头明明是合法的」。

    这里模拟「某个库在 main() 执行期间往 stdout 乱写」：
    把 `json.loads` 换成会先 print 一行再干活的版本 —— 它在 runner 抢下 stdout
    **之后**才被调用（`main()` 第一行就是它），正好落在保护窗口里。
    """
    import json as _json
    import runpy
    import subprocess
    import sys as _sys

    wrapper = (
        "import json, runpy, sys\n"
        "real = json.loads\n"
        "def loud(s, *a, **k):\n"
        "    print('LIBRARY-STRAY-OUTPUT')\n"       # 模拟第三方库乱写
        "    return real(s, *a, **k)\n"
        "json.loads = loud\n"
        "try:\n"
        "    runpy.run_path(r'%s', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
    ) % ragas_service.runner_script()

    proc = subprocess.run([_sys.executable, "-c", wrapper], input=b'{"samples": []}',
                          capture_output=True, cwd=str(ragas_service.runner_script().parents[2]))
    out = proc.stdout.decode("utf-8", "replace").strip()

    # stdout 必须是**干净的一个 JSON**；那行杂音只能出现在 stderr
    data = _json.loads(out)
    assert data["ok"] is True and data["rows"] == []
    assert "LIBRARY-STRAY-OUTPUT" in proc.stderr.decode("utf-8", "replace")


def test_runner_only_writes_results_through_emit():
    """源码级护栏：结果只能走 `_emit()`，不许再出现「直接写 sys.stdout」的回退。"""
    src = ragas_service.runner_script().read_text(encoding="utf-8")
    assert "_emit(" in src
    # 除了「抢下真 stdout」那两行与注释，不该再有别的 sys.stdout 写入
    writes = [ln.strip() for ln in src.splitlines()
              if "sys.stdout" in ln and not ln.strip().startswith("#")]
    # 只允许两件事：开局把真 stdout 抢下来（模块层定义 + __main__ 里再抢一次），
    # 以及把 sys.stdout 让给 stderr。**不许**有任何「写 sys.stdout」的回退。
    assert set(writes) == {"_real_stdout = sys.stdout", "sys.stdout = sys.stderr"}, writes
