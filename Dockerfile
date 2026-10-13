# ============================================================
# grounded-qa 应用镜像
# ------------------------------------------------------------
# ⚠️ 这个镜像**在 CI 构建 → 推 ACR → 服务器只 pull**。
#    绝不在 2C2G 服务器上 docker build（必 OOM）。见 AGENTS 硬约束 §2.4。
#
# ⚠️ 基础镜像不要用 latest（不可复现）；大陆拉不动 Docker Hub，
#    走 daemon registry mirror 或先把基础镜像推进 ACR。
# ============================================================

# ---------- 构建阶段：只用来装依赖 ----------
FROM python:3.13.1-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv：官方静态二进制，比 pip 快得多
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /build

# 先只拷依赖清单，让这层能被缓存
COPY pyproject.toml uv.lock ./

# 装到项目内的 .venv，方便整目录拷走
RUN uv sync --frozen --no-install-project --no-dev

# ---------- 运行阶段：只带运行时 ----------
FROM python:3.13.1-slim-bookworm AS runtime

# 时区与编码；tini 负责正确转发信号（uvicorn 优雅退出要靠它）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv
COPY --chown=appuser:appuser app /app/app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

# 单 worker：2C2G 上不要开多进程（内存预算见计划书 §2.3）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
