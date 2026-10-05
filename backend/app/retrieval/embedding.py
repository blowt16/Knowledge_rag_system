"""嵌入客户端（§3.4.1）。

批量向量化：**20 chunk / 18000 字一批**，指数退避重试。

⚠️ 换任何嵌入模型都要**全量重建索引**（§C.2.1）：
   维度一致 ≠ 可以混用 —— 新旧向量不在同一个语义坐标系里，混着检索会完全失效。

实测（2026-10-05）：`qwen3.7-text-embedding` 返回 **1024 维**，
与 Chroma collection 一致。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import cfg, env, secret

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


def _endpoint() -> tuple[str, str, str]:
    base = secret("embedding.aliyun_base_url").rstrip("/")
    key = secret("embedding.aliyun_access_key")
    model = cfg("embedding.model", "qwen3.7-text-embedding")
    return base + "/embeddings", key, model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    url, key, model = _endpoint()
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=120,
    )
    if r.status_code != 200:
        raise EmbeddingError(f"嵌入接口 HTTP {r.status_code}: {r.text[:200]}")
    data = r.json().get("data")
    if not data:
        raise EmbeddingError("嵌入接口返回空 data")
    # 按 index 排序，避免服务端乱序返回
    items = sorted(data, key=lambda d: d.get("index", 0))
    return [item["embedding"] for item in items]


def _batches(texts: list[str]) -> list[list[str]]:
    max_count = int(cfg("embedding.batch_max_count", 20))
    max_chars = int(cfg("embedding.batch_max_chars", 18000))
    out: list[list[str]] = []
    current: list[str] = []
    chars = 0
    for text in texts:
        if current and (len(current) >= max_count or chars + len(text) > max_chars):
            out.append(current)
            current, chars = [], 0
        current.append(text)
        chars += len(text)
    if current:
        out.append(current)
    return out


async def embed_texts(texts: list[str], *, max_retries: int | None = None) -> list[list[float]]:
    """批量嵌入，带指数退避重试。"""
    if not texts:
        return []
    retries = max_retries if max_retries is not None else int(cfg("embedding.max_retries", 3))

    vectors: list[list[float]] = []
    for batch in _batches(texts):
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                vectors.extend(await asyncio.to_thread(_embed_sync, batch))
                last_error = None
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < retries:
                    delay = 2 ** attempt
                    logger.warning("嵌入批次失败，%s 秒后重试（第 %s 次）：%s",
                                   delay, attempt + 1, e,
                                   extra={"event": "embedding.retry"})
                    await asyncio.sleep(delay)
        if last_error is not None:
            raise EmbeddingError(f"嵌入失败（已重试 {retries} 次）：{last_error}") from last_error

    if len(vectors) != len(texts):
        raise EmbeddingError(f"嵌入返回条数不符：期望 {len(texts)}，实得 {len(vectors)}")
    return vectors


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]


def dimension() -> int:
    return int(cfg("embedding.dimension", 1024))


def is_configured() -> bool:
    """密钥齐不齐 —— 走 security.yaml，与取值路径一致。"""
    try:
        secret("embedding.aliyun_base_url")
        secret("embedding.aliyun_access_key")
    except (KeyError, RuntimeError):
        return False
    return True
