# grounded-qa

> 状态：**v1.0 骨架搭建中**（可运行的最小骨架已就位）｜ 最后更新 2026-10-14

**多租户知识库问答平台** —— 客服是它的第一个业务场景。
深度锚是**检索质量与幻觉治理**：混合检索（全文 + 向量 → RRF 融合 → API 精排）、
带引用溯源的生成、低置信度**拒答**，以及一套**闭卷/开卷对照**的评测体系。

> **它解决什么问题**：把企业私有文档变成"能问、能答、答错能查证"的问答服务。
> 公开文档上 RAG 的增益其实有限（模型参数化记忆已覆盖），
> 真正的价值集中在**私有 / 长尾 / 版本敏感**的知识上 —— 这个项目会把这个结论**如实报出来**，不粉饰。

---

## 目录

- [它是什么](#它是什么)
- [架构](#架构)
- [技术栈](#技术栈)
- [快速上手](#快速上手)
- [目录结构](#目录结构)
- [配置](#配置)
- [部署](#部署)
- [评测](#评测)
- [开发约定](#开发约定)
- [许可](#许可)

---

## 它是什么

一个可以**真正部署上线**的多租户知识库问答服务，面向企业客服场景：

| 能力 | 说明 |
|---|---|
| **多租户隔离** | 所有业务数据带 `tenant_id`；知识库、会话、配额按租户隔离 |
| **混合检索** | PostgreSQL 全文检索 + pgvector 向量检索，用 **RRF** 融合，再走 API 精排 |
| **引用溯源** | 回答附带原文出处，可点开核对，而不是"信我" |
| **低置信拒答** | 检索不到 / 分数低于阈值时**明说不知道**，不编 |
| **文档入库** | 上传 → Redis 队列 → 后台 worker 解析/切分/向量化（不阻塞请求） |
| **可观测** | `/healthz` + `/metrics`，Prometheus 抓取，自研看板 |
| **评测体系** | 闭卷（关 RAG）/ 开卷（开 RAG）对照，双知识库，badcase 归因 |

**安全红线**：**服务端不执行任何用户提交的代码或命令。** 所有模型调用走外部 API，不在服务器上跑本地模型。

---

## 架构

```
                    ┌──────────────────────────────┐
   访客 / 工作台 ──▶ │  nginx :8080                 │
                    │  反代 · 限流 · 静态缓存 · SSE 不缓冲 │
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  自研 API（FastAPI）          │
                    │  意图路由 → 混合检索 → 精排    │
                    │  → 生成（引用溯源 + 拒答）      │
                    │  SSE 流式返回                 │
                    └───┬───────────────┬──────────┘
                        ▼               ▼
            ┌────────────────┐  ┌──────────────┐
            │ PostgreSQL 16  │  │   Redis      │
            │  + pgvector    │  │ 语义缓存+队列  │
            └───────┬────────┘  └──────┬───────┘
                    ▲                  ▼
                    │        ┌────────────────────┐
                    └────────│ 自研 mini worker    │
                             │ 解析/切分/向量化      │
                             └─────────┬──────────┘
                                       ▼
                            外部 API（对话 / embedding / rerank）
```

**两条设计原则**：

1. **一套知识库、一套向量库、一套 embedding 配置**（换 embedding 模型必须全量重嵌入，否则检索质量会静默变差）；
2. **自研核心对外暴露 OpenAI 兼容接口** → 网页 / 飞书 / 任何客户端零成本接入。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 / 后端 | Python 3.13 · FastAPI · SQLAlchemy |
| 数据库 | PostgreSQL 16 + pgvector |
| 缓存 / 队列 | Redis（语义缓存 + `BLPOP` 队列） |
| 异步任务 | **自研 mini worker**（约 40 行 `BLPOP` 循环，**不是 Celery**） |
| 前端 | Vue 3 + TypeScript + Vite |
| AI 框架 | LangChain 最小集 · Function Calling · MCP Server |
| 容器 | Docker + Compose（全服务 `mem_limit` + `healthcheck`） |
| 网关 | nginx（`:8080`） |
| 监控 | Prometheus + node_exporter + 自研看板 |
| CI/CD | GitHub Actions → 阿里云 ACR → SSH pull → 健康检查 → 失败自动回滚 |
| 模型 | 全部走 API：对话/多模态 = 智谱；embedding/rerank = 百炼 |

---

## 快速上手

### 前置要求

- [uv](https://docs.astral.sh/uv/)（Python 包管理）
- Node.js ≥ 20 + [pnpm](https://pnpm.io/)
- Docker（本地依赖服务一律用 compose 起，**不要装到宿主机**）

### 1. 配置环境变量

```bash
cp .env.example .env
# 然后填上你自己的 API Key（.env 已被 .gitignore 忽略，绝不会进仓库）
```

### 2. 起依赖服务 + 应用

```bash
docker compose up -d          # PostgreSQL + Redis + api + worker
docker compose logs -f api    # 看日志
curl http://127.0.0.1:58000/healthz
```

> ⚠️ **本机端口规划（重要）**
> 开发机上可能同时跑着别的项目（例如 `langgraph-mcp-code-agent`，它占了 `3307` / `6379` / `8888` / `80`），
> 所以本项目把**宿主机端口**整块挪到 5 万段，避免撞车：
>
> | 服务 | 宿主机端口 | 容器内端口 |
> |---|---|---|
> | PostgreSQL | **55432** | 5432 |
> | Redis | **56379** | 6379 |
> | API | **58000** | 8000 |
>
> 从宿主机连（psql / redis-cli / GUI）用左列；容器之间互访用右列（compose 里已固定）。

### 3. 本地跑后端（不开容器）

```bash
uv sync                                              # 装依赖
uv run uvicorn app.main:app --reload --port 8000     # 起服务（宿主机 8000）
```

### 4. 本地跑前端

```bash
cd web
pnpm install
pnpm dev          # http://127.0.0.1:5173（已配好 /healthz 等代理）
```

> 前端代理默认指向**容器版 API**（`127.0.0.1:58000`）。
> 若后端是用 `uv run uvicorn` 跑的（宿主机 8000），先设一下再启动：
> `$env:VITE_API_TARGET = "http://127.0.0.1:8000"; pnpm dev`

### 5. 检查与测试

```bash
uv run ruff check .                  # lint
uv run ruff format --check .         # 格式
uv run pytest -q                     # 测试（不需要 docker）
cd web && pnpm build                 # 前端类型检查 + 构建
```

---

## 目录结构

```
app/
├─ api/          HTTP 路由 / SSE 流式
├─ core/         配置、鉴权、多租户、日志、计量
├─ rag/          解析 → 切分 → 嵌入 → 检索 → 精排 → 生成 → 拒答
├─ channels/     渠道适配层（web / feishu）—— v1.0 只预留
├─ db/           表模型、迁移、仓储层
└─ workers/      异步任务（自研 mini worker）
web/             前端（Vue 3 + TypeScript + Vite）
evals/           评测体系（题集 / 断言 / 跑批 / 报告）
tests/           测试
scripts/         run（本地启动）/ kb（知识库入库）
deploy/          nginx / 生产 compose / 服务器初始化
docs/            architecture.md · evidence/ · records/ · archive/
.github/         CI/CD
```

---

## 配置

**所有可调项走环境变量，不硬编码。** 完整清单见 [`.env.example`](.env.example)，关键项：

| 变量 | 说明 |
|---|---|
| `LLM_MODEL` / `LLM_API_KEY` | 对话模型（智谱） |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | embedding（百炼）。**这是元数据锁**：换模型必须全量重嵌入 |
| `RETRIEVE_TOP_K` / `RERANK_TOP_N` / `RRF_K` | 检索与融合参数 |
| `ANSWER_MIN_SCORE` | 低于该分数**拒答** |
| `RATE_LIMIT_PER_MINUTE` / `TOKEN_QUOTA_PER_DAY` | 限流与配额 |

---

## 部署

服务器是**阿里云 ECS 2 核 2G**，内存是硬约束，因此：

- 每个容器都设 **`mem_limit`**（预算见 `docs/`）；
- 镜像**在 CI 构建 → 推 ACR → 服务器只 `pull`**，绝不在服务器上 `docker build`；
- 部署走 GitHub Actions：`ssh` → `compose pull` → `up -d` → 轮询 `/healthz` → **失败自动回滚**；
- 服务按 **`:8080`** 规划（不做 HTTPS）。

部署配置见 [`deploy/`](deploy/)。

---

## 评测

评测体系是项目的**深度锚**，不是附属品。

- **双知识库**：A = 公开文档（证明工程规模）；**B = 自造语料（证明检索真的有效）**。
  公开文档早在大模型训练数据里，检索到没检索到都能答对 —— 所以**成绩看 B 库**。
- **闭卷 / 开卷对照**：同一套题，分别关掉和打开 RAG，看差值。
  预期结论是 **A 库差值小、B 库差值大**，且这个结论会**如实报告**。
- **四档断言强度**：文本 → 轨迹 → 状态 → 评分。
- 测试与评测**一律 mock / 回放 / 缓存**，不真调付费 API。

---

## 开发约定

- **提交**：小步、语义化前缀（`feat:` / `fix:` / `docs:` / `chore:`）
- **格式 / lint**：ruff（`line-length = 100`）
- **测试**：pytest（`asyncio_mode = "auto"`）
- **换行**：`.gitattributes` 固定 `eol=lf`（本机 Windows、CI/服务器 Linux，不固定就会红）
- **文本文件**：一律 **UTF-8 无 BOM**

---

## 许可

MIT
