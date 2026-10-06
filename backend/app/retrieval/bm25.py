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

⚠️ **分词结果必须与映射表同文件落盘**（v2 起）。
   2026-10-05 实测的坑：原先分词结果只存在**进程内缓存**里，而 `add()` 重建索引时
   要从缓存取老文档的 token —— 进程一重启缓存就空，于是所有老文档被写成空 token，
   **从索引里静默消失**：不报错、不降级，只是"召回少一点"。
   现场实测：映射表 186 条里 156 条 token 为 0，唯一现存文档的 6 个 chunk 全是 0，
   拿它自己的正文去搜，前 10 名全是已删除文档的残留条目 —— 混合检索等于单路。
   因此 v1 的落盘文件（只有 version + chunk_ids）**一律判为不可用**。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import jieba

from app.core.config import data_dir

logger = logging.getLogger(__name__)

INDEX_VERSION = 2


@dataclass
class Bm25Hit:
    chunk_id: str
    score: float


def _index_dir() -> Path:
    path = data_dir() / "bm25s"
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
    """从磁盘加载 {version, chunk_ids, tokens}。缺失或不匹配返回 None。"""
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

    chunk_ids = payload.get("chunk_ids") or []
    tokens = payload.get("tokens")
    # 两者条数必须一致：错位会让 add() 把 A 文档的 token 写到 B 文档名下，
    # 返回错误的 chunk —— 比降级更糟，所以判为整路不可用
    if not isinstance(tokens, list) or len(tokens) != len(chunk_ids):
        logger.warning("BM25 映射表与分词结果条数不符，视为不可用",
                       extra={"event": "bm25.index_invalid"})
        return None

    # 落盘索引与映射表必须同代：条数对不上就是按位置错位，
    # 返回的是**别人的 chunk** —— 比降级糟得多，直接判整路不可用
    num_docs = _index_num_docs()
    if num_docs is not None and num_docs != len(chunk_ids):
        logger.warning(
            "BM25 索引与映射表不同代（索引 %s 篇 / 映射表 %s 条），视为不可用",
            num_docs, len(chunk_ids), extra={"event": "bm25.index_invalid"})
        return None

    _state = payload
    return _state


def _index_num_docs() -> int | None:
    """BM25S 落盘索引里**实际收录**的文档数（params.index.json 的 num_docs）。

    ⚠️ 它不是冗余信息：空 token 的文档会被 bm25s **跳过**，落盘索引里根本没有它们。
       M1 开工前的现场就是「索引 num_docs=12，而映射表 186 条」——
       于是 `search()` 拿索引里的下标去查 186 条的映射表，**取回来的是别人的 chunk_id**。
       所以它必须与 `len(chunk_ids)` 一致，不一致就是错位，整路不可用。
    """
    try:
        raw = (_index_path() / "params.index.json").read_text(encoding="utf-8")
        return int(json.loads(raw)["num_docs"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _drop_cache() -> None:
    global _state
    _state = None


def is_available() -> bool:
    """索引与映射表同生共死 —— 任一缺失或版本不匹配即该路不可用。

    ⚠️ 即使 `_state` 已缓存，也要重新确认两个文件仍在：
       删掉文件后还报可用，上层会以为这一路是好的。
    """
    if not _index_path().exists() or not _mapping_path().exists():
        _drop_cache()
        return False
    return _load_state() is not None


def _persist(chunk_ids: list[str], tokens: list[list[str]]) -> None:
    import bm25s

    retriever = bm25s.BM25()
    retriever.index(tokens, show_progress=False)
    retriever.save(str(_index_path()))

    payload = {"version": INDEX_VERSION, "chunk_ids": chunk_ids, "tokens": tokens}
    _mapping_path().write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    global _state
    _state = payload


def _clear_files() -> None:
    """索引空了就把落盘文件删掉 —— 空索引等于没有索引。

    ⚠️ 不能写成「保存一个空索引」：BM25S 对全空语料会在
       `tokenize` 阶段抛 `ValueError: max() iterable argument is empty`
       （实测：删掉最后一份文档时崩在这里）。
    """
    path = _index_path()
    if path.exists():
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    _mapping_path().unlink(missing_ok=True)
    _drop_cache()


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

    state = _load_state()

    # ⚠️⚠️ 「磁盘上有索引但我不敢用」**不等于**「还没有索引」。
    #   把前者当后者从零重建，会造出一个只含本次新增文档的索引 ——
    #   **已有文档被静默清空**，且重建后 is_available() 立刻报健康，
    #   从头到尾不报错。这正是 B-1（重启后老文档从索引里消失）的同一症状、
    #   另一扇门，而且 INDEX_VERSION 一升级就会踩到（M1 刚升过一次）。
    #   宁可让这次上传失败（流水线会补偿删除并把任务置 failed —— 可见、可排查），
    #   也不要静默毁掉已有索引。
    if state is None and _mapping_path().exists():
        raise RuntimeError(
            "BM25 索引存在但不可用（版本不匹配 / 索引与映射表不同代 / 文件损坏）——"
            "拒绝在它之上做增量重建，以免静默清空已有文档。"
            "请重建索引：删掉 data/bm25s 后重新入库。"
        )

    state = state or {}
    existing_ids = list(state.get("chunk_ids") or [])
    existing_tokens = list(state.get("tokens") or [])

    kept_ids: list[str] = []
    kept_tokens: list[list[str]] = []
    for i, cid in enumerate(existing_ids):
        if cid not in incoming:
            kept_ids.append(cid)
            kept_tokens.append(existing_tokens[i])

    for cid, text in incoming.items():
        kept_ids.append(cid)
        kept_tokens.append(tokenize(text))

    _persist(kept_ids, kept_tokens)
    return len(incoming)


def document_chunk_ids(document_id: str) -> list[str]:
    """BM25 索引里属于该文档的全部 chunk_id（按 chunk_id 前缀取）。

    ⚠️ **存在的理由**（M1 的 K-3）：删文档时**不能靠 Chroma 反推 chunk_id** ——
       Chroma 先被清空的话推出来的是空列表，BM25 那批条目就成了
       「还搜得到」的孤儿（不报错，只让召回悄悄变差）。
       索引自己记着它有哪些 chunk，问它最可靠，而且与 Chroma 的当前状态无关。
       chunk_id 的形状是 `{document_id}:{chunk_index}`（`chunker.make_chunk_id`），
       所以前缀匹配是精确的、不会误伤别的文档。
    """
    state = _load_state()
    if not state:
        return []
    prefix = f"{document_id}:"
    return [cid for cid in state["chunk_ids"] if cid.startswith(prefix)]


def remove_document(chunk_ids: list[str]) -> int:
    """按 chunk_id 列表移除（删文档时同步更新索引与映射表）。"""
    if not chunk_ids or not is_available():
        return 0
    drop = set(chunk_ids)
    state = _load_state()
    existing_ids = list(state["chunk_ids"])
    existing_tokens = list(state["tokens"])

    kept_ids, kept_tokens = [], []
    for cid, tokens in zip(existing_ids, existing_tokens):
        if cid in drop:
            continue
        kept_ids.append(cid)
        kept_tokens.append(tokens)

    removed = len(existing_ids) - len(kept_ids)
    if kept_ids:
        _persist(kept_ids, kept_tokens)
    else:
        _clear_files()          # 删空了 —— 留个空索引会让 BM25S 崩
    return removed


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
    """清掉进程内状态 —— 测试用来模拟「进程重启」。"""
    _drop_cache()
