# 校园 RAG 后端镜像（§10.2 M5-5「部署四件」之一）
#
#   docker build -t campus-rag .
#   docker run --env-file .env -p 8090:8090 \
#     -v "$PWD/models:/app/models:ro" -v campus_data:/app/data campus-rag
#
# ⚠️ **`--workers 1` 是硬约束**（A10）：Chroma 内嵌（PersistentClient 直接打开
#    data/chromadb/chroma.sqlite3），BM25S 的磁盘索引同理。换 PostgreSQL 也不行。
#    写成可配置的等于给"调大一点会不会快些"留了后门，所以这里**写死**。
#
# ⚠️ **模型不进镜像**：reranker 2.2 GB，打进镜像既慢又没法复用。
#    用 volume 挂进 /app/models（见上面的 run 命令）。
#
# ⚠️ **数据要挂卷**：data/ 下有 Chroma 索引与 BM25 索引，容器重建就没了。

FROM python:3.13-slim

# git 是 uv 装某些包时要用的；构建完可以不留（这里图简单就不做多阶段了）
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只拷依赖清单 —— 依赖没变时这一层能命中缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./backend/
COPY alembic.ini ./ 2>/dev/null || true

ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

EXPOSE 8090

# 容器里 PG 是另一个服务，健康检查由编排负责；这里只检查进程能应答
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health').status==200 else 1)"

# ⚠️ 不带 --reload：Windows 上 reload 会把事件循环切成 Selector，
#    asyncio 子进程（评测调 ragas 隔离环境）会直接 NotImplementedError。
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "app.main:app", \
     "--workers", "1", "--host", "0.0.0.0", "--port", "8090"]
