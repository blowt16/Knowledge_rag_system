"""索引的构建与失效（§3.4.4 / §3.6）。

⚠️ **写入顺序**（§3.3.1）：先插 `documents` 行（status=indexing）→ Chroma → BM25S
   → **最后把 status 翻成 active**。

⚠️ **失败处理是「补偿删除」，不是事务回滚** —— 跨异构存储（PG / Chroma / BM25S）
   **做不到原子**。

⚠️ **删除顺序**：先删索引（Chroma → BM25S），**最后才动 PostgreSQL**；
   且失败路径下 PG 侧只**置状态**，**行永远保留**
   （`failed` 行承载「传过但失败了」这个事实，删了管理端就分不清
   「没传过」和「传失败了」——而这两种情况的处置完全不同）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.deps import ALL_ROLES
from app.ingestion.chunker import make_chunk_id
from app.retrieval import bm25, vector
from app.retrieval.embedding import embed_texts

logger = logging.getLogger(__name__)


@dataclass
class IndexEntry:
    chunk_id: str
    chunk_index: int
    text: str
    metadata: dict[str, Any]


async def build_index(entries: list[IndexEntry]) -> int:
    """写入 Chroma（含向量）+ BM25S。返回写入的 chunk 数。"""
    if not entries:
        return 0

    texts = [e.text for e in entries]
    vectors = await embed_texts(texts)

    vector.upsert(
        ids=[e.chunk_id for e in entries],
        embeddings=vectors,
        documents=texts,
        metadatas=[e.metadata for e in entries],
    )

    bm25.add([(e.chunk_id, e.text) for e in entries])
    return len(entries)


def remove_from_index(document_id: str, chunk_ids: list[str] | None = None) -> dict[str, int]:
    """补偿删除：**先 Chroma、后 BM25S**（§3.3.1，与写入顺序完全相反）。

    注意这里**不动 PostgreSQL** —— PG 侧只置状态，行要保留。
    """
    removed_chroma = vector.delete_document(document_id)

    if chunk_ids is None:
        chunk_ids = [make_chunk_id(document_id, i) for i in range(removed_chroma)]
    removed_bm25 = bm25.remove_document(chunk_ids)

    logger.info("补偿删除完成", extra={
        "event": "index.compensate_delete",
        "node": "ingest",
    })
    return {"chroma": removed_chroma, "bm25": removed_bm25}


def rewrite_metadata(document_id: str, *, status: str, visibility: str,
                     visible_roles: list[str] | None,
                     effective_date: str) -> int:
    """重写该文档全部 chunk 的 metadata —— **不重新嵌入**（§4.3.1.2）。

    `visibility` / `effective_date` / **`status`** 冗余在 Chroma 里，
    改 PostgreSQL **不会**自动同步。

    ⚠️ `status` 也必须重索引 —— 这条容易漏：原方案的「改后须重新索引」
       只覆盖了 visibility/effective_date。**停用文档却不重写 metadata，
       检索期的 `status = "active"` 过滤就形同虚设。**
    """
    rows = vector.get_chunks(document_id, limit=100000)
    if not rows:
        return 0

    roles = set(visible_roles or [])
    ids: list[str] = []
    metas: list[dict[str, Any]] = []
    for row in rows:
        meta = dict(row["metadata"])
        meta["status"] = status
        meta["visibility"] = visibility
        from app.retrieval.filters import date_key
        meta["effective_date"] = date_key(effective_date)
        for role in ALL_ROLES:
            meta[f"vis_{role}"] = role in roles
        # 去掉读取时解析出来的派生字段，避免写回时结构不对
        meta.pop("chunk_id", None)
        ids.append(row["chunk_id"])
        metas.append(meta)

    vector.update_metadata(ids, metas)
    return len(ids)


def reindex_document(document_id: str, *, status: str, visibility: str,
                     visible_roles: list[str] | None, effective_date: str) -> int:
    """兼容别名 —— 语义同 rewrite_metadata。"""
    return rewrite_metadata(
        document_id,
        status=status,
        visibility=visibility,
        visible_roles=visible_roles,
        effective_date=effective_date,
    )


def index_stats() -> dict[str, int]:
    return {"vector": vector.count(), "bm25": bm25.count()}
