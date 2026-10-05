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
from dataclasses import dataclass

from pathlib import Path

from app.core.config import cfg, repo_path
from app.graph.state import Chunk

logger = logging.getLogger(__name__)

_model = None
_load_failed = False

# GPU 信号量：节点内的「显存不足 → 降级」是单请求内判断，
# 跨请求没有信号量会直接 OOM（§3.2.4）
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(int(cfg("reranker.max_concurrency", 2)))
    return _semaphore


@dataclass
class RerankResult:
    chunks: list[Chunk]
    confidence: float | None
    degraded: bool
    degraded_kind: str | None = None


def _model_dir() -> str:
    """模型路径必须相对**仓库根**解析。

    配置里写的是 `models/BAAI/bge-reranker-v2-m3`（相对仓库根），
    而服务运行的 cwd 是 `backend/` —— 直接用相对路径会 FileNotFoundError，
    表现为「每次请求都静默降级」，很难和「模型真的坏了」区分开。
    """
    raw = cfg("reranker.model_path", "models/BAAI/bge-reranker-v2-m3")
    path = Path(raw)
    if path.is_absolute() and path.exists():
        return str(path)
    resolved = repo_path(*Path(raw).parts)
    return str(resolved if resolved.exists() else path)


def _load_model():
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    try:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(
            _model_dir(),
            max_length=int(cfg("reranker.max_length", 512)),
            device=cfg("reranker.device", "cuda"),
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
    """启动时预热（可选）。加载失败不影响服务启动 —— 只是该路降级。"""
    return _load_model() is not None


def _predict(query: str, chunks: list[Chunk]) -> list[float]:
    model = _model
    batch_size = int(cfg("reranker.batch_size", 10))
    pairs = [(query, c.text[:2000]) for c in chunks]
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    return [float(s) for s in scores]


async def rerank(query: str, chunks: list[Chunk], *, top_k: int | None = None) -> RerankResult:
    """精排。任何异常都降级，**绝不让请求失败**。"""
    top_k = top_k or int(cfg("retrieval.top_k", 5))
    if not chunks:
        return RerankResult(chunks=[], confidence=None, degraded=False)

    async with _get_semaphore():
        try:
            model = await asyncio.to_thread(_load_model)
            if model is None:
                return RerankResult(chunks=chunks[:top_k], confidence=None,
                                    degraded=True, degraded_kind="model_load_failed")

            scores = await asyncio.to_thread(_predict, query, chunks)
            if not scores:
                raise RuntimeError("精排返回空分")

            ranked = sorted(zip(chunks, scores), key=lambda p: p[1], reverse=True)
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
