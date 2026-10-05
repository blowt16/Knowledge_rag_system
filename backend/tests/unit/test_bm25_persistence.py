"""BM25S 索引的落盘与「进程重启」语义（§3.6 / §3.4.3）。

⚠️ 本文件锁死 2026-10-05 实测发现的缺陷：

   `add()` / `remove_document()` 重建索引时，老文档的**分词结果只从进程内缓存取**。
   进程一重启缓存即空 → 所有老文档被写成空 token，从索引里**静默消失**：
   不报错、不降级，只是「召回少一点」。

   实测现场（M1 开工前）：映射表 186 条 / 31 个 document_id，而库里只有 1 个文档；
   186 条里 **156 条的 token 数为 0**，**唯一现存文档的 6 个 chunk 全是 0** ——
   拿它自己的正文去搜，前 10 名全是已删除文档的残留条目。

⚠️ 索引与映射表**同生共死**：任一缺失或版本不匹配即整路不可用，不做部分恢复。

⚠️ 这些用例全部在 tmp_path 上跑，**不碰仓库真实的 data/bm25s**。
"""

from __future__ import annotations

import json

import pytest

from app.retrieval import bm25


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path, monkeypatch):
    """把索引目录指到 tmp_path，并在用例前后清掉进程内状态。

    前置的 `reset_cache()` 不能省：同一次 pytest 会话里，别的用例可能已经加载过
    真实索引，`_state` 会带着真实数据进来。后置的同样不能省 —— 否则下一个用例
    （如 test_pipeline）会拿到本用例的临时索引。
    """
    monkeypatch.setattr(bm25, "_index_dir", lambda: tmp_path)
    bm25.reset_cache()
    yield
    bm25.reset_cache()


def _restart() -> None:
    """模拟进程重启：进程内状态全部丢失，只剩磁盘上的文件。"""
    bm25.reset_cache()


def _hits(query: str, k: int = 10) -> list[tuple[str, float]]:
    return [(h.chunk_id, h.score) for h in bm25.search(query, k=k)]


def _score(query: str, chunk_id: str) -> float | None:
    """该 chunk 对 query 的 BM25 分。

    ⚠️ 判据是**分数 > 0**，不是「在不在结果里」：bm25s 在 k ≥ 语料条数时
       会把所有文档都返回，没命中的那些分数为 0。用「在不在」当判据，
       一个文档即使被清成空 token 也照样"在结果里"，测不出问题。
    """
    for cid, score in _hits(query, k=1000):
        if cid == chunk_id:
            return score
    return None


def _assert_matches(query: str, chunk_id: str) -> None:
    score = _score(query, chunk_id)
    assert score is not None, f"{chunk_id} 已不在索引结果里"
    assert score > 0, f"{chunk_id} 对 {query!r} 的 BM25 分为 0 —— 它已从索引里消失"


# ---- 基本落盘 ----------------------------------------------------------

def test_index_survives_restart_and_returns_same_chunk_ids():
    bm25.rebuild([("a:0", "本科生缓考申请流程与条件"), ("b:0", "学籍管理规定")])
    before = _hits("缓考")

    _restart()

    assert _hits("缓考") == before, "重启后同一 query 应命中同一批 chunk_id"


def test_chinese_query_hits_via_jieba():
    """★ jieba 修复的证据：`str.split()` 时代中文检索是完全失效的。"""
    bm25.rebuild([("a:0", "本科生缓考申请流程与条件"), ("b:0", "学籍管理规定")])
    _assert_matches("缓考", "a:0")          # 分了词才可能命中
    assert _score("缓考", "b:0") == 0.0     # 不相干的文档不该得分


# ---- 增量更新（本缺陷的主场）------------------------------------------

def test_add_after_restart_keeps_old_chunks_searchable():
    """★ 核心回归：重启后调 `add()`，老文档不能被清成空 token。

    修复前本用例的失败形态是「`缓考` 一条都搜不到」——
    老文档的 token 被写成了空列表，安静地退出索引。
    """
    bm25.rebuild([("a:0", "本科生缓考申请流程与条件")])
    _assert_matches("缓考", "a:0")

    _restart()                                  # 进程重启，内存缓存清空
    bm25.add([("c:0", "新文档：学籍管理规定")])   # 再传一份新文档

    _assert_matches("缓考", "a:0")               # ← 修复前这里为 0 分
    _assert_matches("学籍", "c:0")


def test_remove_after_restart_keeps_other_chunks_searchable():
    """同一个根因的另一半：`remove_document()` 也是从同一个缓存取老文档的 token。

    修复前：清一次测试数据，索引里剩下的文档**全部**变成空 token。
    """
    bm25.rebuild([("a:0", "本科生缓考申请流程"), ("b:0", "学籍管理规定")])

    _restart()
    removed = bm25.remove_document(["b:0"])

    assert removed == 1
    _assert_matches("缓考", "a:0")     # 删除无关条目时不该把别的文档清空


# ---- 同生共死 ----------------------------------------------------------

def test_missing_mapping_makes_index_unavailable(tmp_path):
    bm25.rebuild([("a:0", "缓考")])
    assert bm25.is_available()

    (tmp_path / "mapping.json").unlink()

    assert not bm25.is_available(), "映射表没了必须整路不可用，不能做部分恢复"


def test_missing_index_dir_makes_index_unavailable(tmp_path):
    bm25.rebuild([("a:0", "缓考")])
    for f in (tmp_path / "index").iterdir():
        f.unlink()
    (tmp_path / "index").rmdir()

    assert not bm25.is_available()


def test_version_mismatch_makes_index_unavailable(tmp_path):
    """老版本落盘文件必须被判为不可用 —— 这正是 M1 从 v1 升到 v2 的护栏。

    v1 的 mapping.json 里**没有 tokens 字段**；若还当它可用，
    下一次 `add()` 就会把老文档全部写成空 token。
    """
    bm25.rebuild([("a:0", "缓考")])
    payload = json.loads((tmp_path / "mapping.json").read_text(encoding="utf-8"))
    payload["version"] = bm25.INDEX_VERSION + 1
    (tmp_path / "mapping.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    bm25.reset_cache()
    assert not bm25.is_available()


def test_v1_payload_without_tokens_is_rejected(tmp_path):
    """真实历史文件：v1 只有 version + chunk_ids。升版后必须判为不可用。"""
    bm25.rebuild([("a:0", "缓考")])
    (tmp_path / "mapping.json").write_text(
        json.dumps({"version": 1, "chunk_ids": ["a:0"]}, ensure_ascii=False),
        encoding="utf-8")

    bm25.reset_cache()
    assert bm25.INDEX_VERSION != 1, "升版后本用例才有意义"
    assert not bm25.is_available()


# ---- 索引与映射表必须互相对得上 ----------------------------------------

def test_token_count_mismatch_is_rejected(tmp_path):
    """★ mapping 里 tokens 与 chunk_ids 条数不等 → 整路不可用。

    条数不等的后果不是「少几条」，而是**按位置取到别人的 token**：
    add() 会把 A 文档的分词写到 B 文档名下，检索回来的是错的 chunk。
    """
    bm25.rebuild([("a:0", "缓考"), ("b:0", "学籍")])
    payload = json.loads((tmp_path / "mapping.json").read_text(encoding="utf-8"))
    payload["tokens"] = payload["tokens"][:1]          # 少一条
    (tmp_path / "mapping.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    bm25.reset_cache()
    assert not bm25.is_available()


def test_index_doc_count_mismatch_is_rejected(tmp_path):
    """★ BM25S 索引里实际收了 N 篇，映射表却写 M 条 → 整路不可用。

    真实现场（M1 开工前实测）：归档的 `params.index.json` 写着 `num_docs=12`，
    而同期的 `mapping.json` 有 **186** 条 chunk_id。
    因为空 token 的文档会被 bm25s **跳过**，落盘索引里根本没有它们 ——
    于是 `search()` 拿到的下标去查 186 条的映射表，**取回来的是别人的 chunk_id**。
    这比「召回少」更糟：它返回的是错的东西，而且一切看起来都正常。
    """
    bm25.rebuild([("a:0", "缓考"), ("b:0", "学籍")])
    payload = json.loads((tmp_path / "mapping.json").read_text(encoding="utf-8"))
    payload["chunk_ids"].append("c:0")                 # 映射表多一条，索引里没有
    payload["tokens"].append(["退役"])
    (tmp_path / "mapping.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    bm25.reset_cache()
    assert not bm25.is_available()


# ---- 磁盘上有索引但不可用时，增量重建必须被拒绝 ------------------------

def test_add_refuses_when_index_on_disk_is_unusable(tmp_path):
    """★★ 不能把「有索引但我不敢用」当成「还没有索引」从零重建。

    修复前的失败形态：磁盘上留着上一个版本的索引 →
    `is_available()` 判它不可用（正确）→ 但 `add()` 把 `_load_state() is None`
    读成「空库」→ 重建出一个**只含本次新增文档**的索引，
    **已有文档被静默清空**，且随后 `is_available()` 还报健康。

    这正是 B-1（重启后老文档从索引里消失）的同一症状、另一扇门，
    而且 `INDEX_VERSION` 一升级就会踩到 —— M1 刚升过一次。
    """
    bm25.rebuild([("old:0", "本科生缓考申请流程")])
    _assert_matches("缓考", "old:0")

    # 模拟「磁盘上的索引是上一代格式」（v1：只有 version + chunk_ids）
    (tmp_path / "mapping.json").write_text(
        json.dumps({"version": 1, "chunk_ids": ["old:0"]}, ensure_ascii=False),
        encoding="utf-8")
    bm25.reset_cache()
    assert not bm25.is_available(), "前置条件：这份索引应被判为不可用"

    with pytest.raises(RuntimeError, match="不可用"):
        bm25.add([("new:0", "新文档：学籍管理规定")])


def test_add_still_works_on_a_truly_empty_slot(tmp_path):
    """反向对照：从没建过索引时，add() 必须照常工作（别把正常路径也堵死）。"""
    assert not (tmp_path / "mapping.json").exists()
    assert not bm25.is_available()

    bm25.add([("a:0", "本科生缓考申请流程")])

    _assert_matches("缓考", "a:0")
