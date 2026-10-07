"""评测集迁移（§3 / §9）—— 幂等与回填。

⚠️ **幂等不是洁癖**：`cli.py` 的 `init-db` 每次都按序重跑 `migrations/*.sql`。
   不幂等有两个后果：
     ① 重跑报错，中断整条迁移链；
     ② 上线后有人把某题的「来源」改成手工录入，运维重跑一次 `init-db`
        就被改回「文档生成」—— **静默改用户数据**。
   所以下面既测「重跑两次结果一样」，也测「用户改过的值重跑不会被覆盖」。
"""

from __future__ import annotations

import asyncpg
import pytest

from app import db
from app.core.config import BACKEND_DIR, pg_dsn

MIGRATION = BACKEND_DIR / "migrations" / "004_eval_sets.sql"
MIGRATIONS = BACKEND_DIR / "migrations"

#: 迁移前就存在的那批题（= 生成器 fixture 的 id 列表）。迁移只认这批。
LEGACY_IDS = [
    "f-01-01", "r-01", "n-01", "mt-mt-01", "res-01", "x-03",
]

DEFAULT_SET = "eval-set-default"
CALIB_SET = "eval-set-refusal-calib"


async def _apply(conn) -> None:
    # 与 `cli.py::cmd_init_db` 同一条路：asyncpg 无参数时走 simple query protocol，
    # 可执行多语句脚本。
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))


async def _snapshot(conn) -> dict:
    """三张表的内容快照 —— 幂等要的是「内容不变」，不只是行数不变。"""
    cases = await conn.fetch(
        "SELECT id, set_id, source, in_eval, note FROM eval_cases ORDER BY id")
    sets_ = await conn.fetch("SELECT id, name FROM eval_sets ORDER BY id")
    runs = await conn.fetch("SELECT id, set_id, set_name, total_cases, done_cases, error "
                            "FROM eval_runs ORDER BY id")
    return {
        "cases": [tuple(r) for r in cases],
        "sets": [tuple(r) for r in sets_],
        "runs": [tuple(r) for r in runs],
        "case_count": len(cases),
        "set_count": len(sets_),
        "run_count": len(runs),
    }


def test_migration_file_exists():
    assert MIGRATION.exists(), f"缺迁移文件：{MIGRATION}"


async def test_running_twice_changes_nothing():
    """连续跑两次，三张表的内容**逐行**不变（§9 的幂等要求）。"""
    async with db.tx() as conn:
        await _apply(conn)
        first = await _snapshot(conn)
        await _apply(conn)
        second = await _snapshot(conn)

    assert first == second
    assert first["set_count"] >= 2, "两个默认评测集没建出来"


async def test_default_sets_exist_with_stable_ids():
    async with db.tx() as conn:
        await _apply(conn)
        rows = await conn.fetch("SELECT id, name FROM eval_sets WHERE id = ANY($1::text[])",
                                [DEFAULT_SET, CALIB_SET])
    got = {r["id"]: r["name"] for r in rows}
    assert got == {DEFAULT_SET: "默认题库", CALIB_SET: "拒答校准小集"}


async def test_legacy_cases_are_attached_to_a_set():
    """迁移后老题都归了集 —— 否则新界面里一条都看不见。"""
    async with db.tx() as conn:
        await _apply(conn)
        orphans = await conn.fetchval(
            "SELECT count(*) FROM eval_cases WHERE set_id IS NULL")
        misgrouped = await conn.fetchval(
            "SELECT count(*) FROM eval_cases "
            " WHERE suite = 'refusal_calib' AND set_id <> $1", CALIB_SET)
    assert orphans == 0
    assert misgrouped == 0


async def test_legacy_cases_are_marked_generated():
    """决策 19：老 90 条一律标「文档生成」—— 它们都不是在界面上手工敲的。"""
    async with db.tx() as conn:
        await _apply(conn)
        rows = await conn.fetch(
            "SELECT id, source FROM eval_cases WHERE id = ANY($1::text[])", LEGACY_IDS)
    assert rows, "老题一条都没查到，id 列表对不上"
    assert all(r["source"] == "generated" for r in rows), [dict(r) for r in rows]


async def test_user_edit_to_source_survives_a_rerun():
    """§9 ⑥ 那条警告的回归锁：用户把某题改成「手工录入」，重跑不能被改回去。"""
    victim = LEGACY_IDS[0]
    async with db.tx() as conn:
        await _apply(conn)
        before = await conn.fetchval("SELECT source FROM eval_cases WHERE id = $1", victim)
        await conn.execute("UPDATE eval_cases SET source = 'manual' WHERE id = $1", victim)
    try:
        async with db.tx() as conn:
            await _apply(conn)
            assert await conn.fetchval(
                "SELECT source FROM eval_cases WHERE id = $1", victim) == "manual"
    finally:
        async with db.tx() as conn:
            await conn.execute("UPDATE eval_cases SET source = $2 WHERE id = $1",
                               victim, before)


async def test_backfill_gives_old_results_their_question_and_ground_truth():
    """§9 ⑦（必做，否则老报告全是空的）。

    库里已有的 112 行 question/ground_truth 全是 NULL —— 不回填的话，
    打开任何一轮历史报告，「问题」与「标准答案」两列都是空的。
    """
    async with db.tx() as conn:
        await _apply(conn)
        missing = await conn.fetchval(
            """SELECT count(*) FROM eval_case_results
                WHERE case_id IS NOT NULL AND question IS NULL""")
        total = await conn.fetchval(
            "SELECT count(*) FROM eval_case_results WHERE case_id IS NOT NULL")
    assert total > 0, "库里没有可校验的历史结果行"
    assert missing == 0, f"{missing} 行历史结果的问题快照没有回填"


async def test_results_fk_allows_deleting_a_case():
    """§1 第 6 条那个死结：case_id 改成可空 + ON DELETE SET NULL。"""
    async with db.tx() as conn:
        await _apply(conn)
        nullable = await conn.fetchval(
            """SELECT is_nullable FROM information_schema.columns
                WHERE table_name='eval_case_results' AND column_name='case_id'""")
        # pg_constraint.confdeltype 是 `"char"` 类型，asyncpg 给的是 bytes
        deltype = await conn.fetchval(
            """SELECT confdeltype::text FROM pg_constraint
                WHERE conname='eval_case_results_case_id_fkey'""")
        run_deltype = await conn.fetchval(
            """SELECT confdeltype::text FROM pg_constraint
                WHERE conname='eval_case_results_run_id_fkey'""")
    assert nullable == "YES", "case_id 还是 NOT NULL —— 删用例会被外键挡住"
    assert deltype == "n", "case_id 外键不是 ON DELETE SET NULL"
    assert run_deltype == "c", "run_id 外键不是 ON DELETE CASCADE —— 删 run 会被挡住"


# ============================================================
# 从零安装的那条路（另起一个库跑）
# ============================================================
# ⚠️ 上面那些用例跑在**已迁移过**的库上，`source` 列早就存在 ——
#    于是 `DO $$ IF NOT EXISTS(column) $$` 那个分支**永远不会被执行到**。
#    而「建列 + 回填只跑一次」正是幂等的关键，必须单独验：
#    这里另起一个空库，从 001 跑到 004，把迁移前的老题造出来再迁移。

SCRATCH_DB = "campus_rag_migtest"


def _with_db(name: str) -> str:
    """换掉连接串里的**库名**。

    ⚠️ 不能用 `str.replace("/campus_rag", ...)` —— 用户名也是 campus_rag，
       那样会把用户名一起换掉，报「password authentication failed for user」。
       从右边切一次才是库名。
    """
    return pg_dsn().rsplit("/", 1)[0] + "/" + name


async def _scratch_conn():
    admin = await asyncpg.connect(dsn=_with_db("postgres"))
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        await admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    finally:
        await admin.close()
    return await asyncpg.connect(dsn=_with_db(SCRATCH_DB))


async def _drop_scratch() -> None:
    admin = await asyncpg.connect(dsn=_with_db("postgres"))
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    finally:
        await admin.close()


async def test_fresh_install_backfills_and_stays_put():
    """从零建库：老题标 generated、归好集、历史快照回填；再跑两次全都不动。"""
    try:
        conn = await _scratch_conn()
    except asyncpg.InsufficientPrivilegeError:
        pytest.skip("当前 PG 用户没有 CREATEDB 权限，跳过从零安装验证")
    try:
        for name in ("001_init.sql", "002_user_active.sql", "003_qa_logs_clarify_skipped.sql"):
            await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        # 造「迁移前就存在的老题」——此刻 source 列还不存在
        await conn.execute("INSERT INTO eval_cases (id, question, case_type, suite) "
                           "VALUES ('legacy-1','q1','factual','full')")
        await conn.execute("INSERT INTO eval_cases (id, question, case_type, suite) "
                           "VALUES ('legacy-2','q2','refusal','refusal_calib')")
        await conn.execute("INSERT INTO eval_runs (id, status) VALUES ('run-1','done')")
        await conn.execute("INSERT INTO eval_case_results (id, run_id, case_id) "
                           "VALUES ('res-1','run-1','legacy-1')")

        sql = MIGRATION.read_text(encoding="utf-8")
        await conn.execute(sql)

        rows = {r["id"]: r for r in await conn.fetch(
            "SELECT id, source, set_id FROM eval_cases ORDER BY id")}
        assert rows["legacy-1"]["source"] == "generated"
        assert rows["legacy-1"]["set_id"] == DEFAULT_SET
        assert rows["legacy-2"]["set_id"] == CALIB_SET
        assert await conn.fetchval(
            "SELECT count(*) FROM eval_sets") == 2
        assert await conn.fetchval(
            "SELECT question FROM eval_case_results WHERE id='res-1'") == "q1"

        # 用户改过的 source 与「只补 NULL」的 set_id，重跑都不得被覆盖
        await conn.execute("UPDATE eval_cases SET source='manual' WHERE id='legacy-1'")
        await conn.execute(sql)
        await conn.execute(sql)
        assert await conn.fetchval(
            "SELECT source FROM eval_cases WHERE id='legacy-1'") == "manual"
        assert await conn.fetchval("SELECT count(*) FROM eval_sets") == 2
        assert await conn.fetchval("SELECT count(*) FROM eval_cases") == 2
    finally:
        await conn.close()
        await _drop_scratch()
