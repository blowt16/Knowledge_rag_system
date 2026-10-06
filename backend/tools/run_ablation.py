"""跑消融主表（§5.3 的 8 行逐项叠加）。

    cd backend && uv run python tools/run_ablation.py            # 全跑
    cd backend && uv run python tools/run_ablation.py --only 0,7 # 只跑第 0/7 行

**走 HTTP API 而不是直接调 service**：后端正持有 Chroma（A10 单写者），
进程内再起一条评测链路会与它抢索引；而且 API 本身有「同时只允许一轮」的
409 保护，天然把 8 行串起来。

## 关于第 8 行与第 7 行（如实写明）

§5.3 的表格里，第 7 行「+hyde」**已经打开了全部六个开关** ——
也就是说 `eval_runs.config` 注释里点名的那六个键，
第 8 行「完整链路」在**配置上与第 7 行完全相同**。

这不是 bug，是那张表的性质：六个增量键对应第 2–7 行，第 8 行没有第七个键可加。
所以第 8 行的作用是**同一配置的第二次运行** —— 它给出一张表里**噪声下限**的
直接测量（同一配置两次跑出来的差值，就是这套指标在本题库上的抖动）。

`rrf_weighted` / `resolve` 两个键按建表注释「不对应叠加表行」，本表不用。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8090"
USER, PW = "m5verify_ecbb0a", "TempM5!verify"

#: 8 行：名字 → 开关。前 6 行逐项叠加，第 7/8 行见模块头说明。
ROWS: list[tuple[str, dict]] = [
    ("1 纯向量检索", {}),
    ("2 +BM25", {"bm25": True}),
    ("3 +RRF", {"bm25": True, "rrf": True}),
    ("4 +精排", {"bm25": True, "rrf": True, "rerank": True}),
    ("5 +verbatim", {"bm25": True, "rrf": True, "rerank": True,
                     "expand_verbatim": True}),
    ("6 +keywords", {"bm25": True, "rrf": True, "rerank": True,
                     "expand_verbatim": True, "expand_keywords": True}),
    ("7 +hyde", {"bm25": True, "rrf": True, "rerank": True,
                 "expand_verbatim": True, "expand_keywords": True,
                 "expand_hyde": True}),
    ("8 完整链路", {"bm25": True, "rrf": True, "rerank": True,
                    "expand_verbatim": True, "expand_keywords": True,
                    "expand_hyde": True}),
]

POLL_S = 10
TIMEOUT_S = 3600


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的行号（1-based），默认全跑")
    ap.add_argument("--role", default="student")
    args = ap.parse_args()

    only = {int(x) for x in args.only.split(",") if x.strip()} or set(range(1, len(ROWS) + 1))

    with httpx.Client(timeout=120) as c:
        tok = c.post(f"{BASE}/api/auth/login",
                     json={"username": USER, "password": PW}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        run_ids: list[str] = []
        for i, (name, cfg) in enumerate(ROWS, start=1):
            if i not in only:
                run_ids.append("")
                continue
            # 等上一轮结束（API 也会用 409 挡住并发的，这里等更省事）
            while True:
                r = c.post(f"{BASE}/api/admin/eval/run", headers=h, json={
                    "name": f"消融-{name}", "suite": "full", "role": args.role,
                    "config": cfg})
                if r.status_code == 202:
                    break
                if r.status_code == 409:
                    time.sleep(POLL_S)
                    continue
                print(f"!! 第 {i} 行起不来：{r.status_code} {r.text[:200]}", file=sys.stderr)
                return 1
            run_id = r.json()["run_id"]
            run_ids.append(run_id)
            print(f"[{i}/8] {name} -> {run_id}（label={r.json()['config_label']}）", flush=True)

            waited = 0
            while waited < TIMEOUT_S:
                time.sleep(POLL_S)
                waited += POLL_S
                d = c.get(f"{BASE}/api/admin/eval/runs/{run_id}", headers=h).json()
                if d["status"] in ("done", "failed"):
                    m = d.get("metrics") or {}
                    print(f"      {d['status']} cases={m.get('cases')} "
                          f"failed={m.get('failed')} recall={m.get('recall_at_k')} "
                          f"mrr={m.get('mrr')} ragas={m.get('ragas')}", flush=True)
                    break
            else:
                print(f"!! 第 {i} 行超时未完成", file=sys.stderr)

        json.dump({"run_ids": [r for r in run_ids if r]},
                  open("../data/tmp/ablation_runs.json", "w", encoding="utf-8"))
        print("RUN_IDS " + ",".join(r for r in run_ids if r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
