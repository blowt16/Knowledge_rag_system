"""多轮用例生成端到端探针（真实 HTTP + 真实 LLM + 真实落库）。

跑法（仓库根，后端跑在 8090）：
    cd backend && uv run python tools/e2e_multiturn_generation_probe.py

做什么：
- 建临时 admin 用户与评测集；
- 对固定 5 份文档各调一次 `/api/admin/eval/sets/{id}/generate`
  （mode=multi、count=4、turns=3、window=3）；
- 统计 created / failed / timeout / 成功率；
- 从后端日志里解析**逐次尝试**的校验拒绝原因（答案不是原文、答案重复、
  首轮短句含代词、LLM 503/超时）；
- 打印成功生成的对话，结果写 logs/e2e_multiturn_generation_report.json；
- 结束时删除临时用户、评测集和用例。

⚠️ 该探针会真实调用 LLM 并写库，但只写临时评测集，结束后清理。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg
import httpx

from app.core.config import pg_dsn
from app.core.security import hash_password

BASE = "http://127.0.0.1:8090"
PASSWORD = "E2eMt@12345"
LOG_PATH = ROOT / "logs" / "backend.out.log"
OUT_PATH = ROOT / "logs" / "e2e_multiturn_generation_report.json"
COUNT = 4
TITLES = [
    "02_学位授予文件",
    "04_普通高等教育本科学生学籍管理规定_2025修订_桂电教2025-27号",
    "05_考试管理规定_桂电教2020-15号",
    "06_本科生管理规定_桂电学2019-24号",
    "07_学生申诉处理办法_2025修订_桂电学2025-7号",
]


def classify(line: str) -> str | None:
    if "首轮过短且含代词" in line:
        return "首轮短句含代词"
    if "答案不是窗口原文" in line:
        match = re.search(r"答案不是窗口原文（([^）]+)）", line)
        return "答案校验: " + (match.group(1) if match else "unknown")
    if "答案重复" in line:
        return "答案重复"
    if "多轮生成调用失败" in line:
        if "503" in line or "too busy" in line:
            return "LLM: 503 服务繁忙"
        if "Timeout" in line or "timed out" in line:
            return "LLM: 超时"
        return "LLM: 其他异常"
    return None


async def main() -> None:
    conn = await asyncpg.connect(pg_dsn())
    uid = uuid.uuid4().hex
    uname = f"e2emt_{uid[:6]}"
    set_id: str | None = None
    report: dict = {"docs": [], "summary": {}, "cases": []}
    try:
        await conn.execute(
            "INSERT INTO users (id,username,password_hash,role,token_version) "
            "VALUES ($1,$2,$3,'admin',0)", uid, uname, hash_password(PASSWORD))
        docs = await conn.fetch(
            "SELECT id,title FROM documents WHERE title = ANY($1::text[]) ORDER BY title",
            TITLES)
        async with httpx.AsyncClient(base_url=BASE, timeout=60) as cli:
            login = await cli.post("/api/auth/login",
                                   json={"username": uname, "password": PASSWORD})
            login.raise_for_status()
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            created_set = await cli.post(
                "/api/admin/eval/sets", headers=headers,
                json={"name": f"E2E多轮拦截-{uid[:6]}"})
            created_set.raise_for_status()
            set_id = created_set.json()["id"]
            offset = os.path.getsize(LOG_PATH)

            for doc in docs:
                started = time.time()
                try:
                    resp = await cli.post(
                        f"/api/admin/eval/sets/{set_id}/generate", headers=headers,
                        json={"document_id": doc["id"], "count": COUNT,
                              "mode": "multi", "turns": 3, "window": 3},
                        timeout=320.0)
                    body = (resp.json() if resp.status_code == 200
                            else {"http_status": resp.status_code,
                                  "error": resp.text[:300]})
                except Exception as exc:  # noqa: BLE001
                    body = {"error": str(exc)[:300]}
                elapsed = round(time.time() - started, 1)
                report["docs"].append({"title": doc["title"], "elapsed_s": elapsed,
                                       "response": body})
                print(f"[{doc['title']}] {elapsed}s requested={body.get('requested')} "
                      f"created={body.get('created')} failed={body.get('failed')} "
                      f"timeout={body.get('timeout')}", flush=True)
                if body.get("reason"):
                    print("   reason:", body["reason"], flush=True)
                await asyncio.sleep(2)

            rows = await conn.fetch(
                "SELECT question,turns FROM eval_cases WHERE set_id=$1 ORDER BY created_at",
                set_id)
            for row in rows:
                turns = row["turns"]
                if isinstance(turns, str):
                    turns = json.loads(turns)
                report["cases"].append({
                    "question": row["question"],
                    "turns": [t.get("question", "") for t in (turns or [])],
                })

            with open(LOG_PATH, "rb") as fh:
                fh.seek(offset)
                raw = fh.read().decode("utf-8", "ignore")
            reasons: Counter[str] = Counter()
            samples: list[str] = []
            for line in raw.splitlines():
                reason = classify(line)
                if reason:
                    reasons[reason] += 1
                    if len(samples) < 12:
                        samples.append(line.strip())

            created = sum(int(d["response"].get("created") or 0) for d in report["docs"])
            failed = sum(int(d["response"].get("failed") or 0) for d in report["docs"])
            timeouts = sum(1 for d in report["docs"] if d["response"].get("timeout"))
            report["summary"] = {
                "documents": len(report["docs"]),
                "requested_total": sum(
                    int(d["response"].get("requested") or 0) for d in report["docs"]),
                "created": created,
                "failed": failed,
                "success_rate": round(created / (created + failed), 4)
                if (created + failed) else None,
                "requests_timed_out": timeouts,
                "attempt_reasons": dict(reasons),
                "log_sample": samples,
            }
            print("\n" + "=" * 80)
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            print("\n生成成功用例:")
            for case in report["cases"]:
                print("-", " || ".join(case["turns"]))
            OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            print("\n报告:", OUT_PATH)
    finally:
        if set_id:
            async with conn.transaction():
                await conn.execute("DELETE FROM eval_cases WHERE set_id=$1", set_id)
                await conn.execute("DELETE FROM eval_sets WHERE id=$1", set_id)
        await conn.execute("DELETE FROM users WHERE id=$1", uid)
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())