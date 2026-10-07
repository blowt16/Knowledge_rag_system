"""评测集与用例的读写（§3.6 / §5.1 / §5.2）。

本文件分两半：
  - **序列化**（导出/导入）—— 命令行 `tools/eval_set_io.py` 与界面的
    「导出评测集」按钮**共用这一份**。两边各写一份迟早会漂，导出的文件就对不上了。
  - **增删改查** —— 评测集与用例的列表/分页/筛选/搜索等。

⚠️ 一个评测集 = **一个 json 文件**，不是目录。

⚠️ 磁盘**不是运行时存储，是导出落脚点**：界面上新建/改用例只写库，
   磁盘上不落东西；只有手动跑一次导出命令（或点一次导出按钮），文件才会被写出来。
   为什么不让文件当存储：界面要分页/筛选/搜索（文件得全读全解析）、历史报告靠外键
   引用用例（文件没有外键）、评测正在跑时改文件行为无法定义、后端多进程同写会打架。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 导出文件的格式版本。将来加字段时靠它区分「老文件」。
EXPORT_VERSION = 1

#: 用例在导出文件里的**字段全集**。
#:
#: ⚠️ **字段必须列全，一个都不能省。** 90 条老题里 **15 条多轮题靠 `turns`**、
#:    受限题靠 `visible_roles`、A 组指标靠 `expected_route` / `should_clarify`。
#:    漏字段 = 这份「题库的 git 存档」**静默损坏**，而且 round-trip 测试还测不出来
#:    （导入按字段写入，缺的列保持原值，看起来"没丢"）。
CASE_FIELDS = (
    "id", "question", "ground_truth", "case_type", "suite",
    "expected_doc_ids", "expected_chunk_ids", "turns", "visible_roles",
    "expected_route", "should_clarify",
    "source", "in_eval", "note",
    "source_document_id", "source_chunk_id", "source_page", "source_snippet",
)

#: 这些列在库里是 JSONB，写进去要先序列化
_JSONB_FIELDS = frozenset({"expected_doc_ids", "expected_chunk_ids", "turns", "visible_roles"})

#: 文件名里必须换掉的字符：Windows 上非法，`/` `\` 还会变成目录分隔符，
#: 换行会让 `Content-Disposition` 头断行（§5.1）。
_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\r\n\t]')


class SetNotFound(LookupError):
    """评测集不存在 —— 接口层译成 404。"""


class SetNameTaken(ValueError):
    """评测集重名 —— 接口层译成 409（`name` 是 UNIQUE）。"""


def safe_filename(name: str) -> str:
    """把评测集名变成安全的文件名/下载名。

    ⚠️ 逐字符替换成 `_`，不是删掉 —— 删掉会让「售后/问题」与「售后问题」
       撞成同一个文件名，导出两个集就互相覆盖。
    """
    cleaned = _UNSAFE_FILENAME.sub("_", name or "").strip()
    return cleaned or "评测集"


def _loads(value: Any) -> Any:
    """asyncpg 把 JSONB 读成 str；已经是 Python 对象时原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def serialize_set(set_row: dict, cases: list[dict], *,
                  exported_at: str | None = None) -> dict:
    """把评测集与它的用例序列化成导出文件的内容（§3.6）。

    **纯函数** —— 命令行与界面走同一条路，差别只在出口：
    CLI 写进 `backend/eval_sets/`，界面走 HTTP 让浏览器存到本地。
    """
    return {
        "version": EXPORT_VERSION,
        "set": {
            "name": set_row["name"],
            "description": set_row.get("description"),
        },
        "exported_at": exported_at or datetime.now(timezone.utc)
                                             .isoformat(timespec="seconds")
                                             .replace("+00:00", "Z"),
        "cases": [
            {field: _loads(case.get(field)) if field in _JSONB_FIELDS
                    else case.get(field)
             for field in CASE_FIELDS}
            for case in cases
        ],
    }


async def export_payload(conn, name: str) -> dict:
    """按**名字**导出评测集（界面下拉里选中的就是名字）。"""
    row = await conn.fetchrow(
        "SELECT id, name, description FROM eval_sets WHERE name = $1", name)
    if row is None:
        raise SetNotFound(name)
    cases = await conn.fetch(
        "SELECT * FROM eval_cases WHERE set_id = $1 ORDER BY id", row["id"])
    return serialize_set(dict(row), [dict(c) for c in cases])


def write_export_file(payload: dict, out_dir: Path) -> Path:
    """把导出内容写成一个 json 文件，返回落盘路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_filename(payload['set']['name'])}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


async def import_payload(conn, payload: dict) -> dict:
    """把导出文件的内容写回库（§3.6 的导入语义）。

    写死的三条，别有歧义：
      - **按 `id` upsert，只写文件里出现的列**，缺的列保持库中原值
      - `set` 按 `name` 找；**找不到就新建**
      - 可重复跑（幂等）
    """
    set_info = payload.get("set") or {}
    name = (set_info.get("name") or "").strip()
    if not name:
        raise ValueError("导出文件里没有 `set.name`")

    row = await conn.fetchrow("SELECT id FROM eval_sets WHERE name = $1", name)
    set_created = row is None
    if set_created:
        set_id = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO eval_sets (id, name, description) VALUES ($1,$2,$3)",
            set_id, name, set_info.get("description"))
    else:
        set_id = row["id"]
        if "description" in set_info:
            await conn.execute(
                "UPDATE eval_sets SET description = $2, updated_at = now() WHERE id = $1",
                set_id, set_info.get("description"))

    created = updated = 0
    for case in payload.get("cases") or []:
        cols = [f for f in CASE_FIELDS if f in case]
        if "id" not in cols:
            raise ValueError("用例缺 `id`，无法 upsert")
        values = [json.dumps(case[f]) if f in _JSONB_FIELDS else case[f] for f in cols]
        exists = await conn.fetchval("SELECT 1 FROM eval_cases WHERE id = $1", case["id"])
        # `set_id` 总是跟着文件走：用例在导出后可能被搬到了别的集
        insert_cols = cols + ["set_id"]
        params = values + [set_id]
        placeholders = ", ".join(f"${i}" for i in range(1, len(params) + 1))
        # ⚠️ 冲突分支**不含 `set_id`**：导入是「把这份存档写成事实」，
        #    归属由文件决定，所以它进 INSERT 分支就够了 —— 但若该行已存在于
        #    **别的**集，我们仍要把它搬过来，故单独 UPDATE 一次。
        await conn.execute(
            f"""INSERT INTO eval_cases ({', '.join(insert_cols)})
                VALUES ({placeholders})
                ON CONFLICT (id) DO UPDATE SET
                  {', '.join(f'{c} = EXCLUDED.{c}' for c in cols if c != 'id')}""",
            *params)
        if exists:
            updated += 1
            await conn.execute("UPDATE eval_cases SET set_id = $2 WHERE id = $1",
                               case["id"], set_id)
        else:
            created += 1

    return {"set_created": set_created, "set_id": set_id,
            "created": created, "updated": updated}


async def import_file(conn, path: Path) -> dict:
    """从磁盘上的 json 文件导入。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return await import_payload(conn, payload)
