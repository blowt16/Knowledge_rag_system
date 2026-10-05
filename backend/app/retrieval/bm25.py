"""BM25S 稀疏检索（§3.6 / §3.4.3）。

| 项 | 现状（旧项目） | 改造后 |
|---|---|---|
| 分词 | `str.split()` —— **中文完全失效** | `jieba.lcut` |
| 库 | rank_bm25（内存、每次全量重建） | **BM25S**（稀疏矩阵、磁盘持久化） |
| 更新 | 无 | 增量索引 + 索引失效检测 |

⚠️ BM25S **没有 metadata** —— 它返回的是自身语料库里的**下标**，不是 document_id。
   因此索引旁边必须持久化一份 **「下标 → chunk_id」映射表**（与索引同生共死）。

⚠️ 映射表粒度**必须是 `chunk_id`，不是 `document_id`**：
   只到文档粒度的话，① 同一 chunk 被两路命中时无法去重
   （RRF 的**去重键就是 `chunk_id`**）；② BM25 侧命中拿不到
   `char_start` / `char_end` / `page`，引用与原文回跳就缺了定位信息。

⚠️ 二者只要有一个缺失或版本不匹配，即视为该路不可用 —— **不做部分恢复**：
   映射表错位会导致返回错误的 chunk，比降级更糟。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import jieba

from app.core.config import repo_path

logger = logging.getLogger(__name__)

INDEX_VERSION = 1


@dataclass
class Bm25Hit:
    chunk_id: str
    score: float


def _index_dir() -> Path:
    path = repo_path("data", "bm25s")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path() -> Path:
    return _index_dir() / "index"


def _mapping_path() -> Path:
    return _index_dir() / "mapping.json"


def tokenize(text: str) -> list[str]:
    """jieba 分词 —— 修掉旧项目 `str.split()` 导致的中文检索完全失效。

    过滤纯空白与单字符标点，保留中文词与字母数字。
    """
    tokens = []
    for token in jieba.lcut(text or ""):
        token = token.strip()
        if not token:
            continue
        if len(token) == 1 and not token.isalnum():
            continue          # 丢掉单字符标点
        tokens.append(token.lower())
    return tokens


# ---- 状态（进程内缓存）-------------------------------------------------

_state: dict | None = None


def _load_state() -> dict | None:
    """从磁盘加载 {chunk_ids, tokens}。缺失或不匹配返回 None。"""
    global _state
    if _state is not None:
        return _state

    index_file, mapping_file = _index_path(), _mapping_path()
    if not index_file.exists() or not mapping_file.exists():
        return None
    try:
        payload = json.loads(mapping_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("version") != INDEX_VERSION:
        logger.warning("BM25 索引版本不匹配，视为不可用",
                       extra={"event": "bm25.index_invalid"})
        return None
    _state = payload
    return _state


def is_available() -> bool:
    """索引与映射表同生共死 —— 任一缺失或版本不匹配即该路不可用。"""
    return _load_state() is not None


def _persist(chunk_ids: list[str], tokens: list[list[str]]) -> None:
    import bm25s

    retriever = bm25s.BM25()
    retriever.index(tokens, show_progress=False)
    retriever.save(str(_index_path()))

    _mapping_path().write_text(
        json.dumps({"version": INDEX_VERSION, "chunk_ids": chunk_ids},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    global _state
    _state = {"version": INDEX_VERSION, "chunk_ids": chunk_ids}
    # tokens 仅用于增量重建，单独缓存（不落盘，可由 chunk 正文重算）
    _tokens_cache["tokens"] = tokens


_tokens_cache: dict[str, list[list[str]]] = {}


def rebuild(entries: list[tuple[str, str]]) -> int:
    """全量重建。`entries` 是 (chunk_id, text) 列表。"""
    chunk_ids = [cid for cid, _ in entries]
    tokens = [tokenize(text) for _, text in entries]
    _persist(chunk_ids, tokens)
    return len(chunk_ids)


def add(entries: list[tuple[str, str]]) -> int:
    """增量添加。BM25S 需要重建索引，但重建很快（本项目语料百级）。

    已有同 chunk_id 的条目会被替换 —— 重传同一文档时不会残留旧条目。
    """
    incoming = {cid: text for cid, text in entries}

    existing_ids = list((_load_state() or {}).get("chunk_ids", []))
    existing_tokens = list(_tokens_cache.get("tokens", []))

    kept_ids: list[str] = []
    kept_tokens: list[list[str]] = []
    for i, cid in enumerate(existing_ids):
        if cid not in incoming:
            kept_ids.append(cid)
            kept_tokens.append(existing_tokens[i] if i < len(existing_tokens) else [])

    for cid, text in incoming.items():
        kept_ids.append(cid)
        kept_tokens.append(tokenize(text))

    _persist(kept_ids, kept_tokens)
    return len(incoming)


def remove_document(chunk_ids: list[str]) -> int:
    """按 chunk_id 列表移除（删文档时同步更新索引与映射表）。"""
    if not chunk_ids or not is_available():
        return 0
    drop = set(chunk_ids)
    existing_ids = list(_load_state()["chunk_ids"])
    existing_tokens = list(_tokens_cache.get("tokens", []))

    kept_ids, kept_tokens = [], []
    for i, cid in enumerate(existing_ids):
        if cid in drop:
            continue
        kept_ids.append(cid)
        kept_tokens.append(existing_tokens[i] if i < len(existing_tokens) else [])

    _persist(kept_ids, kept_tokens)
    return len(existing_ids) - len(kept_ids)


def search(query: str, *, k: int) -> list[Bm25Hit]:
    """全量打分后返回前 k 条。

    ⚠️ BM25 是**全量打分后过滤**，不存在「先取 Top-N 再过滤」的问题 ——
       所以**只有向量路需要过采样**（§3.3.4），BM25 路不需要。
    """
    state = _load_state()
    if state is None:
        return []

    import bm25s

    retriever = bm25s.BM25.load(str(_index_path()))
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    k = min(k, len(state["chunk_ids"]))
    if k <= 0:
        return []

    results, scores = retriever.retrieve([query_tokens], k=k, show_progress=False)

    hits: list[Bm25Hit] = []
    for idx, score in zip(results[0], scores[0]):
        position = int(idx)
        if 0 <= position < len(state["chunk_ids"]):
            hits.append(Bm25Hit(chunk_id=state["chunk_ids"][position],
                                score=float(score)))
    return hits


def count() -> int:
    state = _load_state()
    return len(state["chunk_ids"]) if state else 0


def reset_cache() -> None:
    global _state
    _state = None
    _tokens_cache.clear()
