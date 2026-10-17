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

# uv：用 pip 装，**不从 ghcr.io 拉**。
# ⚠️ 官方推荐写法是 `COPY --from=ghcr.io/astral-sh/uv:...`，
#    但 **GHCR 在大陆拉不动**（AGENTS 硬约束 §2.5），本地构建必挂。
#    pip 走 PyPI（或镜像源），是这里更可靠的路。
RUN pip install --no-cache-dir uv==0.5.11

# ⚠️ venv 必须建在**与运行阶段完全相同的路径** `/app/.venv`。
#    原因：venv 的控制台脚本（uvicorn / alembic …）会把解释器的**绝对路径烧进 shebang**。
#    若在 /build/.venv 建好再拷到 /app/.venv，shebang 仍写着 `#!/build/.venv/bin/python`，
#    而运行镜像里没有 /build → `exec uvicorn failed: No such file or directory`，
#    容器以**退出码 127 无限重启**（报错信息极具误导性：看着像 uvicorn 没装）。
WORKDIR /app

# 先只拷依赖清单，让这层能被缓存
COPY pyproject.toml uv.lock ./

# 装到项目内的 .venv，方便整目录拷走
RUN uv sync --frozen --no-install-project --no-dev

# ---------- 运行阶段：只带运行时 ----------
FROM python:3.13.1-slim-bookworm AS runtime

# ⚠️ PATH 必须带上 .venv/bin，否则 CMD 里的 uvicorn 会 "command not found"（退出码 127）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:$PATH"

# tini：正确转发信号（uvicorn / worker 的优雅退出要靠它）
# procps：提供 pgrep —— worker 的 healthcheck 用它探进程。
#         ⚠️ python:3.13-slim 里**没有 pgrep/ps**（procps 不在 slim 基础镜像中），
#            不装的话 worker 会一直 unhealthy。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini procps \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=appuser:appuser app /app/app
# ⚠️ 迁移文件也**必须**进镜像 —— 部署时要在这个容器里跑 `alembic upgrade head`。
#    只 COPY app/ 的话，容器里没有 alembic.ini 和 migrations/，
#    部署脚本会在"执行迁移"这一步莫名其妙地失败。
COPY --chown=appuser:appuser alembic.ini /app/alembic.ini
COPY --chown=appuser:appuser migrations /app/migrations
# ⚠️ **`scripts/` 与语料也要进镜像**（2026-10-17）：生产上「灌知识库」那一步是
#    在容器里跑 `python scripts/kb/ingest.py`，语料读 /app/evals/corpus。
#    不带它们，部署到"灌库"这步就会 `No such file or directory` ——
#    而且和"迁移文件没进镜像"是同一类错误（本地跑得好好的，容器里就找不到）。
#    语料只有 ~100 KB，题集/结果已在 .dockerignore 里排除，不会把评测材料带上线。
COPY --chown=appuser:appuser scripts /app/scripts
COPY --chown=appuser:appuser evals/corpus /app/evals/corpus
# ⚠️ **评测结果也要进镜像**（2026-10-17）：只读报告页读的就是 `/app/evals/results/*.json`。
#    题集（`evals/questions/`）与 runner（`evals/run.py`）**仍然不进** ——
#    服务器不跑评测。三者边界见 `AGENTS.md §13`。
#    接口只吐**聚合数字**，逐题明细（题干 / 标准答案 / must_hit）不出 HTTP，见 `app/api/reports.py`。
COPY --chown=appuser:appuser evals/results /app/evals/results

# ⚠️ 上传文件的**停放目录**（api 写、worker 读，compose 里挂同一个卷 `gq-uploads`）。
#    这里必须先建好并 chown：
#    **命名卷第一次创建时，Docker 会用镜像里这个目录的属主来初始化空卷**。
#    漏了这步，卷归 root，非 root 的 `appuser` 写不进去 ——
#    表现是"上传直接 500"，而本地不挂卷时完全正常（这类差异最难查）。
RUN mkdir -p /app/uploads && chown appuser:appuser /app/uploads

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

# 单 worker：2C2G 上不要开多进程（内存预算见计划书 §2.3）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
