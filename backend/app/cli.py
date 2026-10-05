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
# eval-retrieval —— 检索基线（M1 验收①：Recall@5 / MRR）
# ============================================================

async def cmd_eval_retrieval(args: argparse.Namespace) -> int:
    """跑检索基线：Recall@5 与 MRR。

    ⚠️ 链路与图一致：两路召回 → 加权 RRF → 精排 Top-K，**不经过 LLM**。
       绕过 LLM 是刻意的：这样指标只反映检索本身，可重复、可解释、不烧钱。

    ⚠️ 除总指标外**单独输出每一路自己的 Recall@5** ——
       不然「混合检索到底有没有增益」无从判断。
       M0 那个 `$lte` bug（向量路 12 次请求全降级）和 M1 这个 BM25 排序被丢掉，
       都是「表面上两路都在跑、实际只有一路有效」，只看总指标发现不了。
    """
    import json

    from app import db
    from app.core.deps import UserContext
    from app.retrieval.fusion import weighted_rrf
    from app.retrieval.reranker import rerank_sync
    from app.retrieval.search import bm25_retrieve, vector_retrieve

    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = BACKEND_DIR / fixture
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    cases = payload["cases"]
    top_k = args.top_k

    def _rank(chunks, expected: str) -> int | None:
        for i, c in enumerate(chunks, start=1):
            if c.document_name == expected:
                return i
        return None

    def _hit_at(chunks, expected: str, k: int) -> bool:
        names = {c.document_name for c in chunks[:k]}
        return expected in names

    await db.init_pool()
    try:
        user = UserContext(id="eval", username="eval", role=args.role, token_version=0)
        rows_out: list[dict] = []
        degradations: list[str] = []
        for case in cases:
            q = case["question"]
            async with db.tx() as conn:
                vec = await vector_retrieve(conn, q, user)
                bm = await bm25_retrieve(conn, q, user)

            fused = weighted_rrf([
                ({"text": q, "target": "vector", "weight": 1.0}, vec),
                ({"text": q, "target": "bm25", "weight": 1.0}, bm),
            ])
            # 用同步入口：批处理不需要并发，且能避开 Windows 上
            # asyncio.to_thread(torch) 在退出时的 0xC000071C（见 reranker.rerank_sync）
            outcome = rerank_sync(q, fused, top_k=top_k)
            ranked = outcome.chunks
            if outcome.degraded:
                degradations.append(outcome.degraded_kind or "unknown")

            rows_out.append({
                "id": case["id"],
                "type": case["type"],
                "question": q,
                "expected": case["expected_document"],
                "rank": _rank(ranked, case["expected_document"]),
                "hit5": _hit_at(ranked, case["expected_document"], top_k),
                "vec_hit5": _hit_at(vec, case["expected_document"], top_k),
                "bm_hit5": _hit_at(bm, case["expected_document"], top_k),
                "top1": ranked[0].document_name if ranked else "(空)",
            })
    finally:
        await db.close_pool()

    n = len(rows_out)
    recall_at_k = sum(1 for r in rows_out if r["hit5"]) / n
    mrr = sum(1.0 / r["rank"] for r in rows_out if r["rank"]) / n
    vec_recall = sum(1 for r in rows_out if r["vec_hit5"]) / n
    bm_recall = sum(1 for r in rows_out if r["bm_hit5"]) / n

    print(f"\n题库 {fixture.name}   共 {n} 题   top_k={top_k}   视角角色={args.role}")
    print(f"  Recall@{top_k} = {recall_at_k:.3f}    MRR = {mrr:.3f}")
    print(f"  ── 单路对照：向量路 Recall@{top_k} = {vec_recall:.3f}   "
          f"BM25 路 Recall@{top_k} = {bm_recall:.3f}")
    print(f"     {_fusion_verdict(recall_at_k, vec_recall, bm_recall, top_k)}\n")
    print(f"  {'id':<7}{'命中':<5}{'rank':<6}{'向量':<5}{'BM25':<6}{'query':<22}期望文档")
    for r in rows_out:
        mark = "✅" if r["hit5"] else "❌"
        print(f"  {r['id']:<7}{mark:<4}{str(r['rank'] or '-'):<6}"
              f"{'✅' if r['vec_hit5'] else '·':<5}{'✅' if r['bm_hit5'] else '·':<6}"
              f"{r['question'][:20]:<22}{r['expected'][:30]}")

    if degradations:
        # ⚠️ 降级时这一轮的排序**不是精排给的**，而是 RRF 顺序。
        #    照写不误的话，产物里那张「链路 = …→ 交叉编码器精排→」的口径表
        #    就是一纸空文，而 M5 要拿它标定阈值。
        print(f"\n⚠️ 精排降级了 {len(degradations)} 次（{sorted(set(degradations))}）——"
              f"本轮排序来自 RRF，**不是**精排结果。")
        print("   因此不写入基线文件。请先修好精排（模型路径 / 显存）再重跑。")
        await asyncio.get_running_loop().shutdown_default_executor()
        return 1

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = Path(repo_root()) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            _baseline_markdown(payload, rows_out, top_k, args.role,
                               recall_at_k, mrr, vec_recall, bm_recall),
            encoding="utf-8")
        print(f"\n已写入 {out}")

    # ⚠️ Windows + torch 实测：不在**事件循环还活着**的时候显式关掉默认线程池，
    #    解释器退出时会在 executor shutdown 里撞成 `0xC000071C`（退出码 127）——
    #    结果其实已经算完、也打印了，但退出码非 0，脚本没法进 CI。
    #    精排是在 `asyncio.to_thread` 里跑的，线程池里留着 torch 的工作线程，
    #    必须在 loop 关闭前 join 掉。
    await asyncio.get_running_loop().shutdown_default_executor()
    return 0


def _fusion_verdict(total: float, vec: float, bm: float, top_k: int) -> str:
    """据实说明融合相对单路的得失 —— 不要写死一句「融合有增益」。

    这是被 M1 的实测打脸的：本轮 BM25 单路已经 1.000，融合后仍是 1.000，
    「有增益」在 Recall@5 这个粒度上根本看不出来（它只体现在 MRR 与排序上）。
    """
    best = max(vec, bm)
    if total > best + 1e-9:
        return "（融合高于任一单路 → 混合检索确有增益）"
    if abs(total - best) < 1e-9:
        gap = "，但排序可能更稳（看 MRR）" if total >= 1.0 else ""
        return f"（融合 = 最好单路 {"BM25" if bm >= vec else "向量"}{gap}）"
    return "（⚠️ 融合低于最好单路 —— 需要查融合/精排环节）"


def _baseline_markdown(payload, rows, top_k, role, recall, mrr, vec_recall, bm_recall) -> str:
    lines = [
        "# 检索基线（M1 验收①）",
        "",
        "> 由 `app.cli eval-retrieval` 生成，可直接重跑复现。",
        "",
        "## 口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 题库 | `backend/tests/fixtures/eval_min20.json`（20 题：文号 5 + 专有名词 15） |",
        f"| 指标 | Recall@{top_k} / MRR，**文档粒度**（期望文档出现在 Top-{top_k} 即算命中） |",
        f"| 视角 | `{role}`（`include_restricted=false`，即不启用 admin 提权） |",
        "| 链路 | 两路召回 → 加权 RRF（k=60，权重全 1.0）→ 交叉编码器精排 → Top-K |",
        "| 语料 | `corpus/guet/` 10 份公文，入库后 148 个 chunk |",
        "| LLM | **不参与** —— 指标只反映检索本身 |",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| **Recall@{top_k}** | **{recall:.3f}**（{sum(1 for r in rows if r['hit5'])}/{len(rows)}） |",
        f"| **MRR** | **{mrr:.3f}** |",
        f"| 向量路单独 Recall@{top_k} | {vec_recall:.3f} |",
        f"| BM25 路单独 Recall@{top_k} | {bm_recall:.3f} |",
        "",
        "> **单路对照的意义**：只有看到「融合 > 任一单路」，才谈得上混合检索有增益。",
        "> M0 的 `$lte` bug 与 M1 的 BM25 排序丢失都表现为「两路看着都在跑、实际只有一路有效」，",
        "> 只看总指标发现不了。**本轮 BM25 单路已经 1.000，融合在 Recall@5 上没有超出它** ——",
        "> 如实记录，不写成「融合有增益」。",
        "",
        "## 逐题",
        "",
        f"| id | 类型 | query | 期望文档 | 命中 | rank | 向量路 | BM25 路 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['type']} | `{r['question']}` | {r['expected']} | "
            f"{'✅' if r['hit5'] else '❌'} | {r['rank'] or '-'} | "
            f"{'✅' if r['vec_hit5'] else '·'} | {'✅' if r['bm_hit5'] else '·'} |")
    lines += ["", "## 已知问题（不计入指标，但要如实记录）", ""]
    lines += [f"- {c}" for c in payload.get("caveat", [])]
    lines += [
        "",
        "## 阈值",
        "",
        "**本轮不设阈值** —— 计划原文即「记录数值即算达标，阈值 M5 标定」。",
        "上述数值是后续所有检索改动的对照基准。",
        "",
    ]
    return "\n".join(lines)


def repo_root() -> Path:
    from app.core.config import repo_path
    return repo_path()


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

    p_eval = sub.add_parser("eval-retrieval", help="检索基线：Recall@5 / MRR")
    p_eval.add_argument("--fixture", default="tests/fixtures/eval_min20.json",
                        help="题库路径（相对 backend/）")
    p_eval.add_argument("--top-k", type=int, default=5)
    p_eval.add_argument("--role", default="student", choices=["student", "staff", "admin"])
    p_eval.add_argument("--out", default="docs/检索基线.md",
                        help="markdown 输出路径（相对仓库根）；传空字符串则不写")

    args = parser.parse_args()
    handlers = {
        "init-db": cmd_init_db,
        "create-admin": cmd_create_admin,
        "check-llm": cmd_check_llm,
        "eval-retrieval": cmd_eval_retrieval,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
