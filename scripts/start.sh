#!/usr/bin/env bash
# 一键起整套：基础设施 + 后端 + 前端（可观测性走 Langfuse 云，无需本地容器）。
#
#   bash scripts/start.sh              # 起全部（后端前台跑，Ctrl+C 停后端）
#   bash scripts/start.sh --infra-only # 只起容器，不起应用（跑测试/CLI 前用这个）
#
# ⚠️ 跑测试或 CLI 之前**必须先停后端**（A10）：Chroma 是嵌入式的，
#    同一份索引不能两个进程同时写。本脚本的 --infra-only 是为此准备的。
#
# ⚠️ 后端单 worker 是**硬约束**，别为了"快一点"改成多 worker：
#    Chroma 的 PersistentClient 直接打开 data/chromadb/chroma.sqlite3，
#    BM25S 的磁盘索引同理。换了 PostgreSQL 也不行。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ ! -f .env ]; then
  echo "缺少 .env —— 先从模板复制：cp .env.example .env 并填好密钥" >&2
  exit 1
fi

echo "==> 起 PostgreSQL"
docker compose up -d --wait postgres

if [ "${1:-}" = "--infra-only" ]; then
  echo "==> 只起基础设施，完成"
  exit 0
fi

echo "==> 起后端（单 worker，8090）"
echo "    前端另开一个终端：cd frontend/web && npx vite --port 5273"
cd "$ROOT/backend"
exec uv run uvicorn app.main:app --workers 1 --port 8090
