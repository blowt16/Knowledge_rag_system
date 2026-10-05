"""引导脚本（附录 D.4）：init-db / create-admin / check-llm。

幂等，可重跑。

⚠️ 运行方式必须让 `app` package 解析到 backend/app（仓库根还有个旧的 app/）：
       cd backend && uv run python -m app.cli <command>
   或  PYTHONPATH=backend uv run python -m app.cli <command>
   在仓库根直接跑 `python -m app.cli` 会解析到**旧代码**。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

import asyncpg

from app.core.config import BACKEND_DIR, cfg, env, require_env, secret
from app.core.security import hash_password

MIGRATIONS_DIR = BACKEND_DIR / "migrations"


# ============================================================
# init-db —— 按序执行 migrations/*.sql（幂等）
# ============================================================

async def cmd_init_db(_args: argparse.Namespace) -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"❌ 在 {MIGRATIONS_DIR} 下没有找到 .sql 迁移文件")
        return 1

    from app.core.config import pg_dsn
    conn = await asyncpg.connect(dsn=pg_dsn())
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            # asyncpg 在无参数时走 simple query protocol，可执行多语句脚本
            await conn.execute(sql)
            print(f"  ✅ {path.name}")

        rows = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
             ORDER BY table_name
        """)
        tables = [r["table_name"] for r in rows]
        print(f"\n共 {len(tables)} 张表：{', '.join(tables)}")

        expected = {
            "users", "documents", "ingestion_tasks", "conversations", "messages",
            "session_locks", "qa_logs", "degradation_events", "refusal_annotations",
            "eval_cases", "eval_runs", "eval_case_results",
        }
        missing = expected - set(tables)
        if missing:
            print(f"❌ 缺少表：{sorted(missing)}")
            return 1
        print("✅ 12 张表齐全（含 session_locks）")
        return 0
    finally:
        await conn.close()


# ============================================================
# create-admin —— 建首个管理员（附录 D.4 第 ② 步）
# ============================================================

async def cmd_create_admin(args: argparse.Namespace) -> int:
    username = args.username or env("ADMIN_USERNAME")
    password = args.password or env("ADMIN_PASSWORD")

    if not username:
        username = input("管理员用户名: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("管理员口令: ")

    if not username or not password:
        print("❌ 用户名与口令都不能为空")
        return 1

    from app.core.config import pg_dsn
    conn = await asyncpg.connect(dsn=pg_dsn())
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username = $1", username
        )
        if existing:
            print(f"ℹ️  管理员 {username} 已存在（id={existing['id']}），跳过。")
            print("   如需改口令，请用管理端「重置口令」接口。")
            return 0

        user_id = uuid.uuid4().hex
        await conn.execute(
            """INSERT INTO users (id, username, password_hash, role, token_version)
               VALUES ($1, $2, $3, 'admin', 0)""",
            user_id,
            username,
            hash_password(password),
        )
        print(f"✅ 已创建管理员 {username}（id={user_id}）")
        print()
        print("⚠️  请立即清掉 .env 里的 ADMIN_PASSWORD ——")
        print("   它只是引导用的初始口令，长期留在环境变量里等于多一份明文凭据。")
        return 0
    finally:
        await conn.close()


# ============================================================
# check-llm —— 关思考自检（对应施工计划 R13）
# ============================================================

async def cmd_check_llm(_args: argparse.Namespace) -> int:
    """发一条探测请求，断言 reasoning_content 为空且 content 非空。

    关思考一旦失效，流式解析器会拿不到 JSON（§3.5.3 节点 9），
    表现是「答案莫名其妙空掉」。这里让它在启动时就暴露。
    """
    import httpx

    base = secret("llm.deepseek_base_url").rstrip("/")
    key = secret("llm.deepseek_api_key")
    model = cfg("llm.model", "deepseek-flash")
    effort = cfg("llm.reasoning_effort", "none")

    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：收到"}],
        "max_tokens": 64,
    }
    if effort:
        body["reasoning_effort"] = effort

    print(f"探测 {base}  model={model}  reasoning_effort={effort!r}")
    try:
        r = httpx.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        print(f"❌ 请求失败：{type(e).__name__}: {e}")
        return 1

    if r.status_code != 200:
        print(f"❌ HTTP {r.status_code}: {r.text[:300]}")
        return 1

    data = r.json()
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    usage = data.get("usage", {})

    print(f"  prompt_tokens={usage.get('prompt_tokens')} "
          f"completion_tokens={usage.get('completion_tokens')}")
    print(f"  content={content[:40]!r}  reasoning_content={len(reasoning)} 字")

    if reasoning:
        print(f"\n❌ 思考模式未关闭（reasoning_content 有 {len(reasoning)} 字）")
        print("   流式解析器只认 content 里的 JSON，必须关掉。")
        print("   备选写法：把 llm.reasoning_effort 换成 thinking: {type: disabled}")
        return 1
    if not content:
        print("\n❌ content 为空 —— 可能是 max_tokens 太小或模型异常")
        return 1

    print("\n✅ 关思考生效，content 正常")
    return 0


# ============================================================

def main() -> int:
    # Windows 控制台默认 GBK，打不出 emoji/中文全角会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="app.cli", description="校园 RAG 引导脚本")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="建表（幂等，按 migrations/*.sql 顺序执行）")

    p_admin = sub.add_parser("create-admin", help="创建首个管理员（幂等）")
    p_admin.add_argument("--username")
    p_admin.add_argument("--password")

    sub.add_parser("check-llm", help="LLM 连接与关思考自检")

    args = parser.parse_args()
    handlers = {
        "init-db": cmd_init_db,
        "create-admin": cmd_create_admin,
        "check-llm": cmd_check_llm,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
