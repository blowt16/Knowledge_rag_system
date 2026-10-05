"""reranker 启动预热的并发语义（§3.5.3 节点 7 / M0 实测新事实 N-1）。

⚠️ 背景：冷启动实测 **23–80 秒**（首次 79.5s / 页缓存热 23s），
   加载后推理只要 0.4–3s。不预热的话第一个知识型提问要等一分多钟 ——
   这是 M0 结束时最大的体验问题。

⚠️ 预热放进 lifespan 之后，**预热线程与请求线程会同时想加载模型**。
   不加锁就会加载两次：显存翻倍（该模型实占 2.3–2.5 GB，实测），
   而且后加载的实例会覆盖掉先加载的那个，预热白做。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.retrieval import reranker

LOAD_WINDOW = 0.05          # 拉长构造窗口，让并发真正重叠


@pytest.fixture(autouse=True)
def _reset_model_state():
    """模型是模块级全局；用例必须自己清理，否则相互污染。"""
    reranker._model = None
    reranker._load_failed = False
    yield
    reranker._model = None
    reranker._load_failed = False


class _FakeCrossEncoder:
    """数实例化次数 —— 这才是「只加载一次」的可观测证据。"""

    instances = 0
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        with _FakeCrossEncoder._lock:
            _FakeCrossEncoder.instances += 1
        time.sleep(LOAD_WINDOW)
        self.args = args


def _patch_cross_encoder(monkeypatch, *, boom: bool = False) -> None:
    _FakeCrossEncoder.instances = 0

    def factory(*args, **kwargs):
        if boom:
            raise RuntimeError("模拟模型路径不存在")
        return _FakeCrossEncoder(*args, **kwargs)

    monkeypatch.setattr("sentence_transformers.CrossEncoder", factory)


# ---- 并发只加载一次 ----------------------------------------------------

def test_concurrent_preload_loads_model_once(monkeypatch):
    """★ 核心：4 个线程同时预热，模型只能被构造一次。

    修复前 `_load_model()` 没有锁，4 个线程都看到 `_model is None`，
    会各自构造一个 —— 本用例的失败形态就是 `instances == 4`。
    """
    _patch_cross_encoder(monkeypatch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reranker.preload(), range(4)))

    assert all(results), "4 次预热都应报告成功"
    assert _FakeCrossEncoder.instances == 1, (
        f"模型被加载了 {_FakeCrossEncoder.instances} 次 —— 并发下没有互斥"
    )
    assert reranker.is_loaded()


def test_request_path_reuses_warmed_model(monkeypatch):
    """预热过之后，请求路径不该再加载一次。"""
    _patch_cross_encoder(monkeypatch)

    assert reranker.preload() is True
    assert reranker._load_model() is not None

    assert _FakeCrossEncoder.instances == 1


# ---- 加载失败不能影响服务启动 ------------------------------------------

def test_preload_failure_is_not_raised(monkeypatch):
    """预热失败只应让精排降级，**绝不能把服务启动带崩**。"""
    _patch_cross_encoder(monkeypatch, boom=True)

    assert reranker.preload() is False
    assert not reranker.is_loaded()


def test_failed_load_is_not_retried(monkeypatch):
    """失败过一次就记住，别让每个请求都去撞一遍加载（会很慢）。"""
    _patch_cross_encoder(monkeypatch, boom=True)

    assert reranker.preload() is False
    assert reranker.preload() is False
    assert _FakeCrossEncoder.instances == 0      # boom 时 factory 直接抛，不建实例
