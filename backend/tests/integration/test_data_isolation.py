"""测试数据隔离 —— 测试产物不能落进**正在用的** `data/`。

⚠️ 为什么要有这个文件（M5 实测）：写入路径原先直接指向仓库的 `data/`
   （`repo_path("data", ...)`），而 `purge_documents_by_id` 只清 PG 行 + Chroma
   + BM25，**不清文件**。连续跑两天后堆出：

       uploads 3401 个 / normalized 3396 个 / extracted_images 2536 个
       而库里只有 11 份文档

   约 9300 项、124MB 是孤儿（已清理）。

   本文件锁住这条：**摄入产物必须落在重定向后的数据目录，仓库的 `data/` 一个字节都不动。**
   重定向机制见 `conftest.py` 的 `_isolated_data_dir`（设 `RAG_DATA_DIR`）。
"""

from __future__ import annotations

import uuid

from app.core.config import data_dir, repo_path
from tests.support import delete_admin, ingest_text_doc, insert_admin, purge_documents


def _body(title: str) -> str:
    return f"【{title}】\n\n隔离测试正文。" + "本段用于凑足分块长度。" * 20


def test_tests_run_against_a_redirected_data_dir():
    """测试必须跑在重定向目录上 —— 否则下面那条锁是假绿的。"""
    assert data_dir().resolve() != repo_path("data").resolve(), \
        "data_dir() 仍指向仓库的 data/ —— 隔离没生效，测试会继续往里堆孤儿"


async def test_ingest_writes_only_into_redirected_dir():
    """摄入产物落在重定向目录，仓库对应位置**一个都不出现**。

    只断言「这份文档的产物在哪」，不做整目录快照比对 ——
    后者会被同时在使用系统的其他进程干扰（A10 要求跑测试前停后端，
    但那是运维约束，不该让用例本身变得脆弱）。
    """
    real = repo_path("data")
    admin_id = await insert_admin()
    title = f"数据隔离_{uuid.uuid4().hex[:8]}"
    try:
        doc_id = await ingest_text_doc(
            admin_id=admin_id, title=title, text=_body(title))
    finally:
        await purge_documents([title])
        await delete_admin(admin_id)

    leaked = {
        "uploads": [p.name for p in (real / "uploads").glob(f"{doc_id}*")],
        "normalized": [p.name for p in (real / "normalized").glob(f"{doc_id}*")],
        "extracted_images": [p.name for p in (real / "extracted_images").glob(f"{doc_id}*")],
    }
    assert not any(leaked.values()), f"产物漏进了仓库的 data/：{leaked}"

    # 产物确实生成了，只是落在重定向目录里
    assert (data_dir() / "normalized" / f"{doc_id}.txt").is_file(), \
        "规范化文本没写进重定向目录 —— 隔离把产物弄丢了"
    assert list((data_dir() / "uploads").glob(f"{doc_id}*")), \
        "源文件没写进重定向目录"
