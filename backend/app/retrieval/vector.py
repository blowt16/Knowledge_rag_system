"""Chroma 封装（§3.3.2 / §3.3.4）。

⚠️ Chroma 是**进程内嵌**的（PersistentClient 直接打开 data/chromadb/chroma.sqlite3），
   所以部署必须 `--workers 1` —— 多 worker 会争抢同一个文件。
   换了 PG 之后「多 worker」看着可行了，**但 Chroma 仍然不支持**。

⚠️ Chroma 只能按自身 metadata 过滤，无法 join PostgreSQL ——
   这是 metadata 必须冗余一份过滤字段的原因。若改成「先查 PG 拿合法文档 ID，
   再用 ID 列表查 Chroma」，文档一多该列表会超出查询上限。

⚠️ metadata 值只能是 str/int/float/bool —— 不能是 None、不能是列表。
   数组类字段（bbox / image_paths）以 JSON 字符串存放，读取时自行解析。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import chromadb

from app.core.config import cfg, data_dir

logger = logging.getLogger(__name__)

_COLLECTION = "rag_collection"
_client: chromadb.ClientAPI | None = None


def _persist_dir() -> Path:
    path = data_dir() / "chromadb"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(_persist_dir()))
    return _client


def get_collection():
    return get_client().get_or_create_collection(
        name=_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def reset_client() -> None:
    global _client
    _client = None


# ---- 写入 --------------------------------------------------------------

def _flatten(meta: dict[str, Any]) -> dict[str, Any]:
    """把 metadata 拍平成 Chroma 能接受的标量。"""
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            out[key] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float, str)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def upsert(ids: list[str], embeddings: list[list[float]],
           documents: list[str], metadatas: list[dict[str, Any]]) -> None:
    if not ids:
        return
    collection = get_collection()
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=[_flatten(m) for m in metadatas],
    )


def update_metadata(ids: list[str], metadatas: list[dict[str, Any]]) -> None:
    """只改 metadata，**不重新嵌入**（§4.3.1.2）。

    `visibility` / `effective_date` / `status` 冗余在 Chroma 里，改 PostgreSQL
    **不会**自动同步；这三者变更后必须重写该文档全部 chunk 的 metadata。
    重新嵌入不是必需的，metadata 必须重写。
    """
    if not ids:
        return
    get_collection().update(ids=ids, metadatas=[_flatten(m) for m in metadatas])


def delete_document(document_id: str) -> int:
    """删掉某文档的全部 chunk。返回删除条数（近似）。"""
    collection = get_collection()
    existing = collection.get(where={"document_id": document_id}, include=[])
    ids = existing.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def count() -> int:
    return get_collection().count()


# ---- 查询 --------------------------------------------------------------

def query(embedding: list[float], *, where: dict[str, Any], n_results: int,
          include_documents: bool = True) -> list[dict[str, Any]]:
    """按向量检索，带 metadata 过滤。

    ⚠️ Chroma 是「先按向量相似度取 Top-N，再按 metadata 过滤」——
       所以过滤后可能远少于 n_results，这正是 §3.3.4 需要过采样的原因。
    """
    if n_results <= 0:
        return []
    include = ["metadatas", "distances"]
    if include_documents:
        include.append("documents")

    collection = get_collection()
    if collection.count() == 0:
        return []

    result = collection.query(
        query_embeddings=[embedding],
        n_results=min(n_results, collection.count()),
        where=where or None,
        include=include,
    )

    ids = (result.get("ids") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    docs = (result.get("documents") or [[]])[0] if include_documents else []

    hits: list[dict[str, Any]] = []
    for i, chunk_id in enumerate(ids):
        meta = dict(metas[i]) if i < len(metas) and metas[i] else {}
        # 还原 JSON 字符串字段
        for key in ("bbox", "image_paths"):
            raw = meta.get(key)
            if isinstance(raw, str):
                try:
                    meta[key] = json.loads(raw)
                except (ValueError, TypeError):
                    meta[key] = [] if key == "image_paths" else None
        hits.append({
            "chunk_id": chunk_id,
            "text": docs[i] if i < len(docs) else "",
            "metadata": meta,
            "distance": dists[i] if i < len(dists) else None,
        })
    return hits


def get_chunks(document_id: str, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """管理端分块预览用（§4.3.2）。

    这是目前唯一能看见 `char_start/char_end`、`current_chapter`、`vis_*`
    这些 metadata 的地方 —— 它们只存在于向量库里，没有别的可视入口。
    """
    collection = get_collection()
    result = collection.get(
        where={"document_id": document_id},
        include=["metadatas", "documents"],
    )
    ids = result.get("ids", [])
    metas = result.get("metadatas", [])
    docs = result.get("documents", [])

    rows = []
    for i, chunk_id in enumerate(ids):
        meta = dict(metas[i]) if i < len(metas) and metas[i] else {}
        for key in ("bbox", "image_paths"):
            raw = meta.get(key)
            if isinstance(raw, str):
                try:
                    meta[key] = json.loads(raw)
                except (ValueError, TypeError):
                    meta[key] = [] if key == "image_paths" else None
        rows.append({
            "chunk_id": chunk_id,
            "text": docs[i] if i < len(docs) else "",
            "metadata": meta,
        })
    rows.sort(key=lambda r: r["metadata"].get("chunk_index", 0))
    return rows[offset:offset + limit]
