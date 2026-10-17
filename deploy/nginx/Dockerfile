# ============================================================
# nginx 镜像：把前端构建产物 + 反代配置一起打包
# ------------------------------------------------------------
# ⚠️ 在 CI 构建 → 推 ACR → 服务器只 pull。
#    绝不在 2C2G 服务器上 docker build。
#
# 构建（仓库根目录为上下文）：
#     docker build -f deploy/nginx/Dockerfile -t <acr>/nginx:<tag> .
# ============================================================

# ---------- 阶段 1：构建前端 ----------
# ⚠️ **Node 版本必须 ≥ 22.13，且和 `.github/workflows/ci.yml` / 本机保持一致。**
#    `web/package.json` 钉了 `packageManager: pnpm@11.7.0`，而 pnpm 11.7 要求 Node ≥ 22.13 ——
#    这里原来是 `node:22.12.0-alpine`，会让 `corepack` 拉下 pnpm 后**直接拒绝运行**，
#    整条 `docker build` 以 exit code 1 结束（2026-10-17 首次部署实测）。
FROM node:24.9.0-alpine AS web-builder

WORKDIR /web

# 先只拷清单，让依赖层可缓存
# ⚠️ pnpm-workspace.yaml 必须一起拷：pnpm 10+ 的 onlyBuiltDependencies 在那里，
#    少了它 pnpm install 会因"忽略构建脚本"而失败。
COPY web/package.json web/pnpm-lock.yaml* web/pnpm-workspace.yaml ./

RUN corepack enable \
    && pnpm install --frozen-lockfile

COPY web/ ./

RUN pnpm build

# ---------- 阶段 2：nginx ----------
FROM nginx:1.27.3-alpine AS runtime

# 站点配置（覆盖官方镜像自带的 default.conf）
RUN rm -f /etc/nginx/conf.d/default.conf
COPY deploy/nginx/grounded-qa.conf /etc/nginx/conf.d/grounded-qa.conf

# 前端静态产物
COPY --from=web-builder /web/dist /usr/share/nginx/html

EXPOSE 8080

# 官方镜像自带 healthcheck 探的是 80，这里改成 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
