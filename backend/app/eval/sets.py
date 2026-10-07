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

import asyncpg

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


# ============================================================
# 增删改查（§5.1 / §5.2）
# ============================================================

#: 全站分页上限（§4.3.1.1）
MAX_PAGE_SIZE = 100

#: `PATCH /cases/{id}` 允许改的列（§5.2）。
#: ⚠️ `source` / `set_id` / `source_*` **不在内** —— 改了就没有「来源」可言了。
EDITABLE_CASE_FIELDS = frozenset({"question", "ground_truth", "in_eval", "note"})


class CaseNotFound(LookupError):
    """用例不存在 —— 接口层译成 404。"""


class SetBusy(RuntimeError):
    """有 run 正在跑这个集 —— 接口层译成 409。"""


async def list_sets(conn) -> list[dict]:
    """评测集列表，每个带**全部**用例数。

    ⚠️ 数的是**全部**用例，不是只数 `in_eval=true` 的（§11.3-4）：
       它与界面上的行数对得上，用户才不会以为丢了数据。
    """
    rows = await conn.fetch(
        """SELECT s.id, s.name, s.description, s.created_at, s.updated_at,
                  (SELECT count(*) FROM eval_cases c WHERE c.set_id = s.id) AS case_count,
                  (SELECT count(*) FROM eval_cases c
                    WHERE c.set_id = s.id AND c.in_eval) AS in_eval_count
             FROM eval_sets s ORDER BY s.created_at, s.name""")
    return [dict(r) for r in rows]


async def create_set(conn, name: str, description: str | None = None,
                     created_by: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("评测集名不能为空")
    if await conn.fetchval("SELECT 1 FROM eval_sets WHERE name = $1", name):
        raise SetNameTaken(name)
    set_id = uuid.uuid4().hex
    try:
        row = await conn.fetchrow(
            """INSERT INTO eval_sets (id, name, description, created_by)
               VALUES ($1,$2,$3,$4) RETURNING *""",
            set_id, name, description, created_by)
    except asyncpg.UniqueViolationError:        # 并发下先查到没有、插入时撞上
        raise SetNameTaken(name) from None
    return dict(row)


async def update_set(conn, set_id: str, *, name: str | None = None,
                     description: str | None = None) -> dict:
    row = await conn.fetchrow("SELECT * FROM eval_sets WHERE id = $1", set_id)
    if row is None:
        raise SetNotFound(set_id)
    sets_ = []
    params: list[Any] = [set_id]
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("评测集名不能为空")
        if await conn.fetchval("SELECT 1 FROM eval_sets WHERE name = $1 AND id <> $2",
                               name, set_id):
            raise SetNameTaken(name)
        params.append(name)
        sets_.append(f"name = ${len(params)}")
    if description is not None:
        params.append(description)
        sets_.append(f"description = ${len(params)}")
    if not sets_:
        return dict(row)
    sets_.append("updated_at = now()")
    try:
        row = await conn.fetchrow(
            f"UPDATE eval_sets SET {', '.join(sets_)} WHERE id = $1 RETURNING *", *params)
    except asyncpg.UniqueViolationError:
        raise SetNameTaken(name or "") from None
    return dict(row)


async def delete_set(conn, set_id: str) -> None:
    """删评测集，**连带它的用例**（`set_id` 外键是 CASCADE）。

    历史 run 不受影响 —— 它的 `set_name` 是快照，`set_id` 置空（决策 14）。
    """
    running = await running_run_id(conn, set_id)
    if running:
        raise SetBusy(f"该评测集正在被评测使用（run {running[:8]}）")
    result = await conn.execute("DELETE FROM eval_sets WHERE id = $1", set_id)
    if result.endswith(" 0"):
        raise SetNotFound(set_id)


async def running_run_id(conn, set_id: str) -> str | None:
    """有没有 run 正在跑这个集（§5.5 的删除守卫）。

    `runner.py` 开工时把题**一次性读进内存**再逐题跑。若跑到一半有人删了这个集
    或其中一条用例，最后落库时 `case_id` 指向已不存在的行 → 外键报错 → **整轮 failed**。
    系统本来就「一次只允许一轮」，所以检查很轻。
    """
    return await conn.fetchval(
        "SELECT id FROM eval_runs WHERE set_id = $1 AND status IN ('pending','running')",
        set_id)


async def list_cases(conn, set_id: str, *, source: str | None = None,
                     q: str | None = None, page: int = 1,
                     page_size: int = 20) -> dict:
    """用例列表：分页 + 按来源筛选 + 按问题搜索（**都走后端**，不前端过滤）。"""
    page = max(1, int(page or 1))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size or 20)))

    where = ["set_id = $1"]
    params: list[Any] = [set_id]
    if source:
        params.append(source)
        where.append(f"source = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"question ILIKE ${len(params)}")
    clause = " AND ".join(where)

    total = await conn.fetchval(f"SELECT count(*) FROM eval_cases WHERE {clause}", *params)
    rows = await conn.fetch(
        f"""SELECT * FROM eval_cases WHERE {clause}
            ORDER BY created_at DESC, id LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
        *params, page_size, (page - 1) * page_size)

    return {"items": [_case_out(dict(r)) for r in rows], "total": total,
            "page": page, "page_size": page_size}


def _case_out(row: dict) -> dict:
    """把 JSONB 列解成 Python 对象再往外给。"""
    for field in _JSONB_FIELDS:
        row[field] = _loads(row.get(field))
    return row


async def create_case(conn, set_id: str, *, question: str,
                      ground_truth: str | None = None, in_eval: bool = True,
                      note: str | None = None) -> dict:
    """手工录入一条用例（§5.2）。

    ⚠️ **弹窗里没有的字段由服务端自己填**，不填就当场报错：
       - `case_type` 是 `NOT NULL` 且**没有 DEFAULT**（`001_init.sql:222`），
         而参考图的弹窗只有 问题/标准答案/参与评测/备注 四个字段。
         手工录入的都是单轮事实题，所以填 `'factual'`。
       - `expected_doc_ids` **留空** —— 弹窗没这一项。代价是这道题的
         `recall_at_k` / `mrr` 恒为空（`None`，不是 0），**不参与均值**（§7.6）。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    if not await conn.fetchval("SELECT 1 FROM eval_sets WHERE id = $1", set_id):
        raise SetNotFound(set_id)

    row = await conn.fetchrow(
        """INSERT INTO eval_cases
             (id, set_id, question, ground_truth, case_type, suite,
              source, in_eval, note, expected_doc_ids)
           VALUES ($1,$2,$3,$4,'factual','full','manual',$5,$6,NULL)
           RETURNING *""",
        uuid.uuid4().hex, set_id, question, ground_truth, in_eval, note)
    return _case_out(dict(row))


async def update_case(conn, case_id: str, **fields) -> dict:
    """改一条用例。**只有 `EDITABLE_CASE_FIELDS` 里的列会被写**，其余静默忽略。"""
    row = await conn.fetchrow("SELECT * FROM eval_cases WHERE id = $1", case_id)
    if row is None:
        raise CaseNotFound(case_id)

    edits = {k: v for k, v in fields.items()
             if k in EDITABLE_CASE_FIELDS and v is not None}
    if "question" in edits:
        edits["question"] = str(edits["question"]).strip()
        if not edits["question"]:
            raise ValueError("问题不能为空")
    if not edits:
        return _case_out(dict(row))

    params: list[Any] = [case_id]
    pieces = []
    for key, value in edits.items():
        params.append(value)
        pieces.append(f"{key} = ${len(params)}")
    row = await conn.fetchrow(
        f"UPDATE eval_cases SET {', '.join(pieces)} WHERE id = $1 RETURNING *", *params)
    return _case_out(dict(row))


async def delete_case(conn, case_id: str) -> None:
    """删用例。**历史结果保留**，只是 `case_id` 置空（外键 ON DELETE SET NULL）。"""
    row = await conn.fetchrow("SELECT set_id FROM eval_cases WHERE id = $1", case_id)
    if row is None:
        raise CaseNotFound(case_id)
    if row["set_id"]:
        running = await running_run_id(conn, row["set_id"])
        if running:
            raise SetBusy(f"该用例所在评测集正在被评测使用（run {running[:8]}）")
    await conn.execute("DELETE FROM eval_cases WHERE id = $1", case_id)


# ---- 来源片段与高亮 --------------------------------------------------

_WHITESPACE = re.compile(r"\s")


def locate_highlight(snippet: str | None, answer: str | None) -> list[int] | None:
    """标准答案在 `snippet` 里的字符区间 `[start, end)`（**end 不含**）。

    ⚠️ **不能直接 `snippet.find(ground_truth)`。** 生成期的校验是
       `app/eval/generate.py::squeeze()` —— **去掉所有空白之后**再比子串。
       原因是规范化正文里有 PDF 提取留下的硬换行
       （`…提出申请并经\\n\\n学院审核同意后送达；`），模型复述时自然写成一行。
       所以标准答案在原文里**往往不是逐字连续子串**，`find()` 会经常返回 -1，
       界面上就永远没有高亮。

       正确做法：**按去空白口径定位，再把区间映射回原文偏移**
       （记录每个非空白字符对应的原文下标，比对成功后取首尾映射回去）。

    对不上返回 `None` —— 那往往是**文档更新过了**，正是这个功能要暴露的（§8.3）。
    """
    if not snippet or not answer:
        return None
    squeezed: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(snippet):
        if not _WHITESPACE.match(ch):
            squeezed.append(ch)
            offsets.append(i)
    target = _WHITESPACE.sub("", answer)
    if not target:
        return None
    pos = "".join(squeezed).find(target)
    if pos < 0:
        return None
    return [offsets[pos], offsets[pos + len(target) - 1] + 1]


async def case_source(conn, case_id: str) -> dict:
    """「核对标准答案」弹窗吃的数据（§5.2 / §6.5）。"""
    row = await conn.fetchrow(
        """SELECT c.*, d.title AS source_document_title
             FROM eval_cases c
             LEFT JOIN documents d ON d.id = c.source_document_id
            WHERE c.id = $1""", case_id)
    if row is None:
        raise CaseNotFound(case_id)
    row = dict(row)
    snippet = row.get("source_snippet")
    return {
        "question": row["question"],
        "ground_truth": row.get("ground_truth"),
        "source_document_id": row.get("source_document_id"),
        "source_document_title": row.get("source_document_title"),
        "source_chunk_id": row.get("source_chunk_id"),
        "source_page": row.get("source_page"),
        "source_snippet": snippet,
        "highlight": locate_highlight(snippet, row.get("ground_truth")),
    }
