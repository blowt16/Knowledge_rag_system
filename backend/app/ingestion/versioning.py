"""版本判定（决策 #5 / §3.4.3）。

```
新文档上传
    ↓
按 **title 精确匹配**已有文档组
    ↓
┌── 匹配到 ──┐          ┌── 未匹配到 ──┐
↓            ↓          ↓              ↓
version =   同组旧版     新建 group
  旧版+1     状态不变       version = 1
↓            ↓          ↓
沿用同一 doc_group_id
    ↓
录入 effective_date（上传时指定，默认今天）
```

⚠️⚠️ **「旧版+1」的基准必须钉死**：取该 `doc_group_id` 下**所有行的 `max(version)`**
   —— **包含 `failed` 与 `indexing` 的行**，不是只取 `status = "active"` 的。

   **为什么**：§3.3.1 规定失败行**永不删除**，它**占着版本号**。
   若只按 `active` 行取最大值，下次上传会分到一个**已被占用**的 version，
   直接撞 `UNIQUE(doc_group_id, version)` —— 而且是那种
   「平时不出现、**出过一次失败上传之后才开始出现**」的错，**最难排查**。

⚠️ 版本分配**必须在 PostgreSQL 事务内**（§3.3.1）：
   否则两个管理员同时上传同一 doc_group 会读到相同的"旧版 version"，
   产生重复版本号。配套 `UNIQUE(doc_group_id, version)` 兜底。

⚠️ 增量更新：MD5 相同 → 直接跳过；MD5 不同但同组 → 按新版本处理。
   **不做 chunk 级 diff**（决策 #5 已明确）。
"""

from __future__ import annotations

import uuid

import asyncpg


async def resolve_group_and_version(
    conn: asyncpg.Connection, title: str
) -> tuple[str, int]:
    """在同一事务内分配 (doc_group_id, version)。

    调用方必须已经开启事务 —— 这是 §3.3.1 的硬要求。
    """
    row = await conn.fetchrow(
        "SELECT doc_group_id, MAX(version) AS max_version "
        "  FROM documents "
        " WHERE title = $1 "
        " GROUP BY doc_group_id "
        " ORDER BY max_version DESC "
        " LIMIT 1",
        title,
    )

    if row is None:
        return uuid.uuid4().hex, 1

    # MAX(version) 覆盖该组全部行：failed 行也占着版本号，不能跳过
    return row["doc_group_id"], int(row["max_version"]) + 1


async def find_existing_by_md5(conn: asyncpg.Connection, md5: str) -> asyncpg.Record | None:
    """按 MD5 判重。

    已存在 → 该文件标记为 `duplicate` 并跳过（§3.3.1）。
    只认**成功过**的行：失败的行不该阻止重新上传 —— 那正是用户想重传的场景。
    """
    return await conn.fetchrow(
        "SELECT id, title, status FROM documents "
        " WHERE md5 = $1 AND status IN ('active', 'disabled', 'indexing') "
        " ORDER BY created_at DESC LIMIT 1",
        md5,
    )
