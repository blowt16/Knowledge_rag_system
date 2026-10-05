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


# ============================================================
# eval-multiturn —— 多轮指代与查询理解（M2 验收）
# ============================================================

async def _drive_to_retrieve(graph, state: dict) -> dict:
    """把图跑到 retrieve 之后**停下**（精排由调用方用同步入口补，见 `_rank_top`）。

    ⚠️ 为什么停在 generate 之前：M2 考的是**查询理解**（消解/路由/澄清/扩展）。
       跑到底会多付一次 LLM 调用，还会把 M3 生成侧的缺陷混进 M2 的指标里 ——
       到时候失败原因分不清是「没消解对」还是「没答好」。

    ⚠️ 为什么用**真实图**而不是在评测器里重搭一条链路：重搭的那份迟早和
       `builder.py` 漂移，而漂移之后指标照样好看、只是不再反映线上行为。
    """
    from app.services.chat_service import node_ran

    final: dict = {}
    async for mode, payload in graph.astream(state, stream_mode=["custom", "values"]):
        if mode != "values":
            continue
        final = payload or {}
        # chat / clarify 会自然走到 END；knowledge 在 retrieve 之后截断
        if node_ran(final, "retrieve"):
            break
    return final


def _rank_top(final: dict, top_k: int) -> tuple[list[str], bool]:
    """按图的最终顺序精排，返回 (文档名列表, 是否降级)。

    ⚠️ 精排走**同步入口** `rerank_sync`，不调图里的 `rerank` 节点 —— 与
       `eval-retrieval` 同一条理由（见 `reranker.rerank_sync` 的说明）：
       Windows 上 `asyncio.run` + `to_thread(torch/CUDA)` 在解释器退出时会于
       executor shutdown 撞 `0xC000071C`（退出码 127）。

       M2 实测把成因收窄了一步：**必须同时存在 asyncpg 连接池**才复现 ——
       只有连接池、或只有 `to_thread(torch)`，两者单独跑都是退出码 0；
       连接池 + torch 在主线程也是 0。评测器天然两者都有，所以踩得到。

       两条路的排序完全相同（`rerank()` 就是 `to_thread(rerank_sync)`），
       差别只有「跑在哪个线程」。
    """
    from app.retrieval.reranker import rerank_sync

    candidates = final.get("candidates") or []
    if not candidates:
        return [], False
    query = final.get("resolved_query") or final.get("query", "")
    outcome = rerank_sync(query, candidates, top_k=top_k)
    return [c.document_name for c in outcome.chunks], outcome.degraded


async def _raw_control(query: str, user_lite, top_k: int) -> tuple[list[str], bool]:
    """对照腿：**不做消解**，其余与主腿完全相同（同样过一次 rewrite）。

    ⚠️ 对照腿必须与主腿**只差一个变量**（评审 I1）。
       最初这里直接拿原句单路 verbatim 检索，而主腿是「消解后 + 三类扩展」——
       一次变了两个变量，0.714 与 1.000 的差就归因不到消解头上，
       而 M5 的消解消融很可能直接复用这套口径，口径错会把调参带偏。
       现在两腿都过 `rewrite_node`，**差别只剩「消解」**。
    """
    from app.graph.nodes.retrieve import retrieve_node
    from app.graph.nodes.rewrite import rewrite_node
    from app.graph.state import new_state

    # 注意：不设 resolved_query —— rewrite 与 rerank 都会回落到 query（原句）
    state = new_state(query=query, session_id="eval-multiturn", user=user_lite)
    state.update(await rewrite_node(state))
    state.update(await retrieve_node(state))
    names, rerank_degraded = _rank_top(state, top_k)

    kinds = [f"control-{e['node']}:{e['degraded']}"
             for e in (state.get("trace") or []) if e.get("degraded")]
    if rerank_degraded:
        kinds.append("control-rerank:degraded")
    return names, kinds


async def cmd_eval_multiturn(args: argparse.Namespace) -> int:
    """多轮指代题库：路由准确率 / 澄清误报率 / 澄清命中率 + 消解对照。

    ⚠️ 与 `eval-retrieval` 的区别：那个绕过图直达检索（指标只反映检索本身）；
       这个**走真实图的前半段**，因为要考的正是图里 resolve/route/rewrite
       三个节点的判断 —— 绕过它们等于什么都没测。
    """
    import json

    from app import db
    from app.core.deps import UserContext
    from app.graph.builder import get_graph
    from app.graph.state import Message, UserContextLite, new_state

    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = BACKEND_DIR / fixture
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    cases = payload["cases"]
    top_k = args.top_k

    await db.init_pool()
    try:
        graph = get_graph()
        user_lite = UserContextLite(id="eval", role=args.role)

        rows_out: list[dict] = []
        rows_by_case: dict[str, list[dict]] = {}
        degradations: list[str] = []

        for case in cases:
            history: list[Message] = []
            last_route = ""
            case_rows: list[dict] = []

            for idx, turn in enumerate(case["turns"], start=1):
                q = turn["question"]
                state = new_state(
                    query=q,
                    session_id="eval-multiturn",
                    user=user_lite,
                    history=list(history),
                    # 与 chat_service 同源：上一轮被路由到哪一类（只有 knowledge 参与粘性）
                    last_route=last_route,
                    include_restricted=False,
                )
                final = await _drive_to_retrieve(graph, state)

                route = final.get("route", "")
                pred_clarify = route == "clarify"
                want_clarify = bool(turn["should_clarify"])
                names, rerank_degraded = _rank_top(final, top_k)
                expected = turn["expected_document"]
                resolved = final.get("resolved_query") or ""
                anchors = turn["resolved_must_contain_any"] or []

                # 只有「需要消解的轮次」才跑对照 —— 它要额外过一次 rewrite + 精排
                raw_names: list[str] | None = None
                if anchors and expected:
                    raw_names, raw_kinds = await _raw_control(q, user_lite, top_k)
                    degradations.extend(raw_kinds)

                for entry in (final.get("trace") or []):
                    if entry.get("degraded"):
                        degradations.append(f"{entry['node']}:{entry['degraded']}")
                if rerank_degraded:
                    degradations.append("rerank:degraded")

                row = {
                    "case": case["id"], "turn": idx, "type": case["type"],
                    "question": q, "probe": bool(turn.get("probe")),
                    "route": route, "expected_route": turn["expected_route"],
                    "route_ok": route == turn["expected_route"],
                    "route_source": final.get("route_source", ""),
                    "pred_clarify": pred_clarify, "want_clarify": want_clarify,
                    "clarify_ok": pred_clarify == want_clarify,
                    "resolved": resolved,
                    "skip_reason": final.get("resolve_skipped_reason", ""),
                    "anchors": anchors,
                    "resolved_ok": (any(a in resolved for a in anchors)
                                    if anchors else None),
                    "expected": expected,
                    "hit": (expected in names) if expected else None,
                    "raw_hit": ((expected in raw_names)
                                if (expected and raw_names is not None) else None),
                    "top1": names[0] if names else "(空)",
                }
                case_rows.append(row)
                rows_out.append(row)

                # 下一轮的历史：本轮用户原话 + 本轮助手回复（澄清问句也算回复）
                history.append(Message(role="user", content=q))
                history.append(Message(role="assistant",
                                       content=final.get("answer") or ""))
                last_route = route

            rows_by_case[case["id"]] = case_rows
    finally:
        await db.close_pool()

    # ---- 汇总 ----------------------------------------------------------
    n = len(rows_out)
    route_acc = sum(1 for r in rows_out if r["route_ok"]) / n

    negatives = [r for r in rows_out if not r["want_clarify"]]
    false_clarify = [r for r in negatives if r["pred_clarify"]]
    clarify_fp = len(false_clarify) / len(negatives) if negatives else 0.0

    missed_clarify = [r for r in rows_out if r["want_clarify"] and not r["pred_clarify"]]

    # 澄清命中率：**期望澄清且确实触发了**的轮次里，紧接着那一轮收敛了没有
    converge: list[bool] = []
    for case in cases:
        rs = rows_by_case[case["id"]]
        for i, r in enumerate(rs[:-1]):
            if r["want_clarify"] and r["pred_clarify"] and rs[i + 1]["expected"]:
                converge.append(bool(rs[i + 1]["hit"]))
    clarify_hit = (sum(converge) / len(converge)) if converge else 0.0

    anchor_rows = [r for r in rows_out if r["anchors"]]
    resolved_ok = (sum(1 for r in anchor_rows if r["resolved_ok"]) / len(anchor_rows)
                   if anchor_rows else 0.0)

    with_target = [r for r in anchor_rows if r["expected"]]
    resolved_hit = (sum(1 for r in with_target if r["hit"]) / len(with_target)
                    if with_target else 0.0)
    raw_hit = (sum(1 for r in with_target if r["raw_hit"]) / len(with_target)
               if with_target else 0.0)

    rule_hits = sum(1 for r in rows_out if r["route_source"] == "rule")
    skip_counts: dict[str, int] = {}
    for r in rows_out:
        skip_counts[r["skip_reason"]] = skip_counts.get(r["skip_reason"], 0) + 1

    timeouts = [r for r in rows_out if r["skip_reason"] == "timeout"]

    # ---- 控制台 --------------------------------------------------------
    print(f"\n题库 {fixture.name}   {len(cases)} 段对话 / {n} 轮   top_k={top_k}   "
          f"视角角色={args.role}")
    print(f"  路由准确率   = {route_acc:.3f}")
    print(f"  澄清误报率   = {clarify_fp:.3f}   （{len(false_clarify)}/{len(negatives)} 个不该澄清的轮次）")
    print(f"  澄清命中率   = {clarify_hit:.3f}   （{sum(converge)}/{len(converge)} 个触发后收敛）")
    print(f"  澄清漏报     = {len(missed_clarify)} 轮")
    print(f"  消解命中率   = {resolved_ok:.3f}   （锚点轮 {len(anchor_rows)} 个）")
    print(f"  ── 消解对照：消解后检索命中 = {resolved_hit:.3f}   "
          f"不消解（原句，下游同链路）= {raw_hit:.3f}"
          f"   （{len(with_target)} 轮有检索目标）")
    print(f"  规则层命中   = {rule_hits}/{n}（其余走 LLM）")
    print(f"  消解跳过原因 = {skip_counts}")
    print()
    print(f"  {'case':<8}{'轮':<4}{'路由':<10}{'期望':<10}{'澄清':<6}{'消解':<6}"
          f"{'检索':<6}{'快照':<5}query")
    for r in rows_out:
        mark = "✅" if r["route_ok"] else "❌"
        clar = ("✅" if r["clarify_ok"] else "❌") if r["want_clarify"] or r["pred_clarify"] else "·"
        res_ok = "·" if r["resolved_ok"] is None else ("✅" if r["resolved_ok"] else "❌")
        hit = "·" if r["hit"] is None else ("✅" if r["hit"] else "❌")
        print(f"  {r['case']:<8}{r['turn']:<4}{r['route']:<10}{r['expected_route']:<10}"
              f"{clar:<6}{res_ok:<6}{hit:<6}{mark:<5}"
              f"{'[探针]' if r['probe'] else ''}{r['question'][:22]}")

    if degradations or timeouts:
        print(f"\n⚠️ 本轮有降级/超时：降级 {sorted(set(degradations))}，"
              f"消解超时 {len(timeouts)} 次。")
        print("   这时的指标**不反映链路的设计行为**，因此不写入评测文件。请先修好再重跑。")
        await asyncio.get_running_loop().shutdown_default_executor()
        return 1

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = Path(repo_root()) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            _multiturn_markdown(payload, rows_out, rows_by_case, top_k, args.role,
                                route_acc, clarify_fp, clarify_hit, missed_clarify,
                                resolved_ok, resolved_hit, raw_hit, rule_hits, n),
            encoding="utf-8")
        print(f"\n已写入 {out}")

    # Windows + torch：必须在事件循环还活着的时候关掉默认线程池（同 eval-retrieval）
    await asyncio.get_running_loop().shutdown_default_executor()
    return 0


def _multiturn_markdown(payload, rows, rows_by_case, top_k, role,
                        route_acc, clarify_fp, clarify_hit, missed, resolved_ok,
                        resolved_hit, raw_hit, rule_hits, n) -> str:
    anchors = [r for r in rows if r["anchors"] and r["expected"]]
    lines = [
        "# 多轮指代与查询理解评测（M2 验收）",
        "",
        "> 由 `app.cli eval-multiturn` 生成，可直接重跑复现。",
        "",
        "## 口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 题库 | `backend/tests/fixtures/eval_multiturn.json`"
        f"（{len(payload['cases'])} 段对话 / {n} 轮） |",
        "| 链路 | **真实图**：resolve → route →（chat/clarify 结束）或 "
        "rewrite → retrieve；精排走**同步入口** `rerank_sync`（同 eval-retrieval），"
        "**在 generate 之前截断** |",
        f"| 视角 | `{role}`（`include_restricted=false`） |",
        "| LLM | 参与（消解 / 路由兜底 / 查询扩展）—— 与 eval-retrieval 的关键区别 |",
        "| 澄清误报率 | 不该澄清却澄清的比例（分母：标了 `should_clarify=0` 的轮次） |",
        "| 澄清命中率 | **确实触发**澄清的轮次里，其下一轮收敛"
        "（route=knowledge 且命中期望文档）的比例 |",
        "| 消解命中率 | 标了锚点的轮次里，`resolved_query` 含该锚点的比例 |",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| **路由准确率** | **{route_acc:.3f}** |",
        f"| **澄清误报率** | **{clarify_fp:.3f}** |",
        f"| **澄清命中率** | **{clarify_hit:.3f}** |",
        f"| 澄清漏报 | {len(missed)} 轮 |",
        f"| 消解命中率 | {resolved_ok:.3f} |",
        f"| 规则层命中（省下的 LLM 调用） | {rule_hits}/{n} |",
        "",
        "## 消解对照（这一节才是「消解有用」的证据）",
        "",
        "只报「消解后命中 = 1.0」说明不了什么 —— 得看**不消解**时会掉到多少。"
        "两腿**只差「消解」这一个变量**：主腿是「消解后 → rewrite → 检索 → 精排」，"
        "对照腿是「原句 → rewrite → 检索 → 精排」，下游完全相同：",
        "",
        "| 轮次 | 原句（不消解） | 消解后 | 期望文档 | 消解结果 |",
        "|---|---|---|---|---|",
    ]
    for r in anchors:
        lines.append(
            f"| {r['case']}#{r['turn']} | {'✅' if r['raw_hit'] else '❌'} | "
            f"{'✅' if r['hit'] else '❌'} | {r['expected']} | `{r['resolved']}` |")
    lines += [
        "",
        f"> 汇总：消解后 Recall@{top_k} = **{resolved_hit:.3f}**，"
        f"不消解（原句，下游同链路）Recall@{top_k} = **{raw_hit:.3f}**"
        f"（{len(anchors)} 轮有检索目标）。",
        "",
        "## 逐轮明细",
        "",
        "| case | 轮 | query | 路由 | 期望路由 | 来源 | 澄清 | 消解跳过原因 | "
        "消解后 | 检索 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        clar = "✅" if r["clarify_ok"] else "❌"
        res_ok = {None: "·", True: "✅", False: "❌"}[r["resolved_ok"]]
        hit = {None: "·", True: "✅", False: "❌"}[r["hit"]]
        probe = "**探针** " if r["probe"] else ""
        lines.append(
            f"| {r['case']} | {r['turn']} | {probe}`{r['question']}` | {r['route']} | "
            f"{r['expected_route']} | {r['route_source']} | {clar} | "
            f"{r['skip_reason']} | {res_ok} `{r['resolved'][:40]}` | {hit} |")

    lines += ["", "## 本卷口径修订记录", ""]
    lines += [f"- {c}" for c in payload.get("revision_note", [])]
    lines += ["", "## 已知问题（题库自带的口径说明）", ""]
    lines += [f"- {c}" for c in payload.get("caveat", [])]
    lines += [
        "",
        "## 阈值",
        "",
        "**本轮不设阈值** —— 计划原文即「多轮指代题通过」；阈值与基线对比留给 M5 标定。",
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

    p_mt = sub.add_parser("eval-multiturn",
                          help="多轮指代与查询理解：路由/澄清/消解（M2 验收）")
    p_mt.add_argument("--fixture", default="tests/fixtures/eval_multiturn.json",
                      help="题库路径（相对 backend/）")
    p_mt.add_argument("--top-k", type=int, default=5)
    p_mt.add_argument("--role", default="student", choices=["student", "staff", "admin"])
    p_mt.add_argument("--out", default="docs/多轮指代评测.md",
                      help="markdown 输出路径（相对仓库根）；传空字符串则不写")

    args = parser.parse_args()
    handlers = {
        "init-db": cmd_init_db,
        "create-admin": cmd_create_admin,
        "check-llm": cmd_check_llm,
        "eval-retrieval": cmd_eval_retrieval,
        "eval-multiturn": cmd_eval_multiturn,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
