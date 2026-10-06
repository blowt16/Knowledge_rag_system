"""ACL 对照实验（§5.3 的「对照实验」表）—— **单独一张表，不进叠加表**。

    cd backend && uv run python tools/run_acl_experiment.py

三腿（§5.3 原文的顺序与判据）：

| 腿 | 角色 | 提权 | 跑哪些题 | 期望 |
|---|---|---|---|---|
| 1 基准 | admin | ✅ | 全部（含受限题） | 受限题**能命中**，且引用带 `escalated` |
| 2 反证 | admin | ❌ | 受限题 | **0 命中** ← 最有说服力的一格 |
| 3 隔离 | student | ❌ | 受限题 | **0 命中** |

> **第 2 腿是这张表的核心**：它证明「admin 不是旁路」—— 同一批受限题，
> admin 不显式提权就拿不到，与 student 结果一致。只有第 1、3 腿的话，
> 「admin 能跑全部题」会被误读成「因为 admin 有旁路」，而不是「因为他显式提权了」。

⚠️ **自建自清**（负责人 2026-10-06 定）：受限文档由本脚本创建、跑完删除 ——
   Chroma 基线因此仍是 148，不会被这次实验改掉。
   用现有文档的副本**不行**：学生检索会命中公开的原件，第 3 腿的「0 命中」就假了。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8090"
USER, PW = "m5verify_ecbb0a", "TempM5!verify"
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "backend" / "tests" / "fixtures" / "restricted_sample.md"
POLL_S = 10


def _wait(c, h, run_id, timeout=3600):
    waited = 0
    while waited < timeout:
        time.sleep(POLL_S)
        waited += POLL_S
        d = c.get(f"{BASE}/api/admin/eval/runs/{run_id}", headers=h).json()
        if d["status"] in ("done", "failed"):
            return d
    raise TimeoutError(run_id)


def main() -> int:
    with httpx.Client(timeout=300) as c:
        tok = c.post(f"{BASE}/api/auth/login",
                     json={"username": USER, "password": PW}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        # ---- 建受限文档（走真实上传入口，走真实的 ACL 写路径）------------
        print("==> 上传受限文档（visibility=restricted, visible_roles=[staff]）")
        with open(SRC, "rb") as f:
            r = c.post(f"{BASE}/api/admin/documents/upload", headers=h,
                       files={"file": (SRC.name, f, "text/markdown")},
                       data={"visibility": "restricted",
                             "visible_roles": json.dumps(["staff"]),
                             "title": "实验室安全内部管理细则"})
        if r.status_code not in (200, 202):
            print(f"!! 上传失败 {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return 1
        task_id = r.json().get("task_id") or r.json().get("id")
        print("   task_id =", task_id)

        # 等入库完成：上传接口没有单任务状态查询，按标题在文档列表里轮询到 active
        doc_id = None
        for _ in range(60):
            time.sleep(5)
            docs = c.get(f"{BASE}/api/admin/documents?q=实验室安全内部管理细则",
                         headers=h).json().get("items") or []
            hit = [d for d in docs if d.get("title") == "实验室安全内部管理细则"]
            if hit and hit[0].get("status") == "active":
                doc_id = hit[0]["id"]
                break
        if not doc_id:
            print("!! 文档没有入库成功", file=sys.stderr)
            return 1
        print("   document_id =", doc_id)

        # ---- 三腿 --------------------------------------------------------
        legs = [
            ("1 基准：admin + 提权（全部题）", "admin", True, None),
            ("2 反证：admin 不提权（受限题）", "admin", False, "restricted"),
            ("3 隔离：student（受限题）", "student", False, "restricted"),
        ]
        out = []
        for name, role, escalated, case_type in legs:
            body = {"name": f"ACL-{name}", "suite": "full", "role": role,
                    "include_restricted": escalated,
                    "config": {"acl_leg": name}}
            if case_type:
                body["config"]["case_type"] = case_type
            rr = c.post(f"{BASE}/api/admin/eval/run", headers=h, json=body)
            if rr.status_code == 409:
                time.sleep(POLL_S)
                rr = c.post(f"{BASE}/api/admin/eval/run", headers=h, json=body)
            rr.raise_for_status()
            run_id = rr.json()["run_id"]
            print(f"[{name}] run_id={run_id}")
            d = _wait(c, h, run_id)
            m = d.get("metrics") or {}
            print(f"    {d['status']} cases={m.get('cases')} "
                  f"unauthorized_hits={m.get('unauthorized_hits')} recall={m.get('recall_at_k')}")
            out.append({"leg": name, "role": role, "escalated": escalated,
                        "run_id": run_id, "status": d["status"],
                        "cases": m.get("cases"),
                        "unauthorized_hits": m.get("unauthorized_hits"),
                        "recall_at_k": m.get("recall_at_k")})

        json.dump(out, open(REPO / "data" / "tmp" / "acl_experiment.json", "w",
                            encoding="utf-8"), ensure_ascii=False, indent=1)

        # ---- 清理（自建自清）--------------------------------------------
        print("==> 删除受限文档")
        dr = c.delete(f"{BASE}/api/admin/documents/{doc_id}", headers=h)
        print("   删除:", dr.status_code)
        print("DONE " + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
