"""Cross-Encoder 精排 + 降级链（§3.5.3 节点 7 / §C.1.4）。

模型 `BAAI/bge-reranker-v2-m3`，本地 GPU 推理，`batch_size=10`，`max_length=512`。

显存实测（2026-10-04，附录 F.1）：
    加载后增量 **2288 MiB**、一次推理后增量 **2502 MiB** → 实占约 **2.3–2.5 GB**，
    **不是 4.4 GB**（那是 torch 的 venv 磁盘体积，磁盘推不出显存占用）。
    6 GB 总量 − 1.3 GB 桌面基线 − 2.5 GB reranker ≈ 仍有 2.3 GB 余量。

⚠️ 降级策略：**任何异常走同一条路径** —— 跳过精排、按 RRF 顺序返回、记降级事件。
   分三种情况写不增加任何实现。它是整条链路里**唯一依赖 GPU 的环节**，
   也是唯一的硬件单点。

⚠️ `retrieval_confidence` 降级时必须填 **`null`**，
   **不能填 RRF 分值** —— RRF 分与 cross-encoder 分量纲不同，
   混在同一字段里会让仪表盘的分布图失去意义。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

from pathlib import Path

from app.core.config import cfg, repo_path
from app.graph.state import Chunk

logger = logging.getLogger(__name__)

_model = None
_load_failed = False

# 加载互斥：预热跑在后台线程，请求路径跑在 to_thread 的工作线程里，
# 两者会同时想加载模型。不加锁 = 加载两次（该模型实占 2.3–2.5 GB，显存直接翻倍），
# 且后加载的实例会覆盖先加载的，预热等于白做。
_load_lock = threading.Lock()

# GPU 信号量：节点内的「显存不足 → 降级」是单请求内判断，
# 跨请求没有信号量会直接 OOM（§3.2.4）
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(int(_pcfg("max_concurrency", 2)))
    return _semaphore


@dataclass
class RerankResult:
    chunks: list[Chunk]
    confidence: float | None
    degraded: bool
    degraded_kind: str | None = None


def backend() -> str:
    """当前精排后端：`cross_encoder`（BAAI bge）或 `jina_listwise`（Jina v3.5）。

    两者的**接口与分数刻度都不同**，见 `_load_model` / `_predict` 的分支说明。
    """
    return str(cfg("reranker.backend", "cross_encoder"))


def _pcfg(key: str, default):
    """读**当前后端**的配置；该后端没写这一项时回退到共用默认。

    为什么要有这层：切换模型时最容易漏改阈值。bge 的 0.3 套到 Jina 上会把
    **全部候选砍光**（2026-10-17 实测，Jina 对相关块也只给 0.28–0.50），
    而那种故障表现为「系统开始什么都拒答」，很难和模型坏了区分开。
    把每一项都挂在 backend 下面，切换就只需要改 `reranker.backend` 一行。

    ⚠️ 回退链是「后端专属 → 共用默认」：cross_encoder 没有专属块，
       所以 `reranker.min_score` 这类既有扁平键**仍然是它的有效来源**
       （`tests/unit/test_reranker_threshold.py` 正是 patch 这个键）。
    """
    value = cfg(f"reranker.{backend()}.{key}")
    return cfg(f"reranker.{key}", default) if value is None else value


def _model_dir() -> str:
    """模型路径必须相对**仓库根**解析。

    配置里写的是 `models/BAAI/bge-reranker-v2-m3`（相对仓库根），
    而服务运行的 cwd 是 `backend/` —— 直接用相对路径会 FileNotFoundError，
    表现为「每次请求都静默降级」，很难和「模型真的坏了」区分开。
    """
    raw = _pcfg("model_path", "models/BAAI/bge-reranker-v2-m3")
    path = Path(raw)
    if path.is_absolute() and path.exists():
        return str(path)
    resolved = repo_path(*Path(raw).parts)
    return str(resolved if resolved.exists() else path)


def _load_model():
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _load_lock:
        # 双重检查：等锁期间可能已被预热线程加载完
        if _model is not None or _load_failed:
            return _model
        try:
            kind = backend()
            max_length = int(_pcfg("max_length", 512))
            device = str(_pcfg("device", "cuda"))

            if kind == "jina_listwise":
                # Jina v3.5 是 **listwise** 模型：仓库自带 `modeling.py`（JinaForRanking），
                # 必须走 `AutoModel.from_pretrained(..., trust_remote_code=True)`，
                # **不能**用 CrossEncoder 加载（架构不是标准分类头）。
                # ⚠️ trust_remote_code 会执行模型自带代码 —— 只加载可信来源的模型。
                from transformers import AutoModel

                loaded = AutoModel.from_pretrained(
                    _model_dir(), dtype="auto", trust_remote_code=True)
                loaded.eval()
                if device.startswith("cuda"):
                    loaded = loaded.to(device)
                _model = loaded
            else:
                from sentence_transformers import CrossEncoder

                _model = CrossEncoder(
                    _model_dir(),
                    max_length=max_length,
                    device=device,
                )
        except Exception:  # noqa: BLE001
            logger.exception("reranker 加载失败，后续请求直接降级",
                             extra={"event": "rerank.load_failed", "node": "rerank"})
            _load_failed = True
            _model = None
    return _model


def is_loaded() -> bool:
    return _model is not None


def preload() -> bool:
    """启动时预热（由 lifespan 在后台线程调用）。

    加载失败**不影响服务启动** —— 只是该路降级（走降级链）。
    并发安全由 `_load_lock` 保证：预热与请求路径只会加载一次。
    """
    return _load_model() is not None


def _predict(query: str, chunks: list[Chunk]) -> list[float]:
    """给候选块打分，返回**与 `chunks` 等长、下标对齐**的分数数组。

    两种后端的接口不同，但**下游完全一致**（排序 → 阈值 → top_k 都在 `rerank_sync`）：
      - `cross_encoder`：`model.predict([(q, doc), ...])` —— pointwise，逐条独立打分
      - `jina_listwise`：`model.rerank(q, [doc, ...])` —— listwise，整批一次前向、
        文档间互相注意，返回 `[{document, relevance_score, index}]`
    """
    model = _model
    if backend() == "jina_listwise":
        rows = model.rerank(query, [c.text[:2000] for c in chunks])
        scores = [0.0] * len(chunks)
        for row in rows:
            i = int(row["index"])
            if 0 <= i < len(scores):
                scores[i] = float(row["relevance_score"])
        return scores

    batch_size = int(_pcfg("batch_size", 10))
    pairs = [(query, c.text[:2000]) for c in chunks]
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    return [float(s) for s in scores]


def rerank_sync(query: str, chunks: list[Chunk], *, top_k: int | None = None) -> RerankResult:
    """精排的**同步**实现。任何异常都降级，**绝不让请求失败**。

    ⚠️ 为什么单独留一个同步入口：Windows 上「`asyncio.run` + `to_thread(torch/CUDA)`」
       在解释器退出时会于 executor shutdown 撞成 `0xC000071C`（实测，退出码 127）。
       命令行批处理（`app.cli eval-retrieval`）不需要并发，在主线程直接跑
       既避开这个问题，也省掉一层线程开销；服务端仍走下面的异步 `rerank()`。
    """
    top_k = top_k or int(cfg("retrieval.top_k", 5))
    if not chunks:
        return RerankResult(chunks=[], confidence=None, degraded=False)

    try:
        model = _load_model()
        if model is None:
            return RerankResult(chunks=chunks[:top_k], confidence=None,
                                degraded=True, degraded_kind="model_load_failed")

        scores = _predict(query, chunks)
        if not scores:
            raise RuntimeError("精排返回空分")

        ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
        # 分数阈值：低于 `reranker.min_score` 的直接丢弃（2026-10-14 实测默认 0.3，
        # 见 tests/unit/test_reranker_threshold.py）。全滤掉时返回空列表、
        # degraded=False —— 节点层据「reranked 为空」走零成本拒答。
        # 降级路径没有精排分，不做过滤（保持 RRF 顺序原样返回）。
        min_score = float(_pcfg("min_score", 0.3))
        ranked = [(c, s) for c, s in ranked if s >= min_score]

        out: list[Chunk] = []
        for chunk, score in ranked[:top_k]:
            chunk.score = float(score)
            out.append(chunk)

        return RerankResult(chunks=out, confidence=float(max(scores)),
                            degraded=False)

    except asyncio.TimeoutError:
        return RerankResult(chunks=chunks[:top_k], confidence=None,
                            degraded=True, degraded_kind="timeout")
    except RuntimeError as e:
        kind = "oom" if "out of memory" in str(e).lower() else "model_load_failed"
        logger.warning("精排失败，降级为 RRF 顺序：%s", e,
                       extra={"event": "rerank.degraded", "node": "rerank"})
        return RerankResult(chunks=chunks[:top_k], confidence=None,
                            degraded=True, degraded_kind=kind)
    except Exception as e:  # noqa: BLE001
        logger.warning("精排异常，降级为 RRF 顺序：%s", e,
                       extra={"event": "rerank.degraded", "node": "rerank"})
        return RerankResult(chunks=chunks[:top_k], confidence=None,
                            degraded=True, degraded_kind="model_load_failed")


async def rerank(query: str, chunks: list[Chunk], *, top_k: int | None = None) -> RerankResult:
    """精排的**异步**入口：把同步实现丢到线程里跑，别阻塞事件循环。

    GPU 信号量在这里 —— 它约束的是**跨请求**的并发（§3.2.4），
    单线程的批处理入口不需要它。
    """
    async with _get_semaphore():
        return await asyncio.to_thread(rerank_sync, query, chunks, top_k=top_k)
