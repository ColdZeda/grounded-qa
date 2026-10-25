# AGENTS.md · 给 AI 助手的操作手册

> 状态：**当前有效** ｜ 最后更新 2026-10-26

**给谁看**：**AI 编码助手**（Claude / Cursor / GLM / DeepSeek / 其他）。**不是**给人类看的。

> 本文件只管一件事：**你在这个工作区里该怎么做。**
> —— 流程怎么展开 · 命令是什么 · 代码风格与约定（**尤其反直觉的**）· 禁区 · 环境要求 · 工作区架构。
>
> **不写**项目介绍（那是 `repo\README.md` 的活）、**不写**决策与规划（那是 `..\plan\PROJECT-PLAN.md` 的活）、**不写**方案辩论过程。

---

## 0. 30 秒进入状态

| 问题 | 答案 |
|---|---|
| 这是什么 | **多租户知识库问答平台**，客服是第一个业务场景；深度锚是**检索质量与幻觉治理** |
| **当前工作区** | **`E:\projects\grounded-qa`** ← 会话的工作区根就是这里，不是 `repo\` |
| 代码仓库 | `E:\projects\grounded-qa\repo`（git）—— 远端是 GitHub **公开**仓库 `ColdZeda/grounded-qa`；**本地分支 `main`，已推送、已配 `origin`**。<br>⚠️ **历史教训留在这里**：本地原名 **`master`**，而 workflow 只在 **`main`** 上触发 —— 直接推 `master` 会让 **CI 一次都不跑、而且不报错**。2026-10-17 已改名为 `main`。<br>⚠️ **GitHub 的网络在大陆是间歇性的**：2026-10-17 推着推着就 `Failed to connect to github.com port 443`，**开代理后立刻恢复**。📌 **推不出去先怀疑代理，别去改 git 配置。** |
| 运行时在哪 | **阿里云「轻量应用服务器」（SWAS）** 2 核 2G（Ubuntu 24.04），公网 `120.24.67.82`<br>⚠️ **不是标准 ECS** —— 见 §10 那条坑（端口在**「防火墙」**里管，不是「安全组」） |
| **完整决策在哪** | **`..\plan\PROJECT-PLAN.md`** —— 唯一有效的计划书，**细节的唯一归属地**（含 §8 功能分期与进度），**动手前应当读一遍** |
| **大方向在哪** | **`..\plan\PHASES.md`** —— **只有一张阶段总览表**（现在哪个阶段、还剩几个）。**刻意保持薄**；细节一律去 §8 |
| **想法放哪** | **`..\plan\CANDIDATES.md`（候选池）** —— 还没定的点子放这里，**只增不删**。<br>⚠️ **想法绝不写进计划书**，计划书只写"已定"的 |
| 计划书版本规则 | `plan\` 里**始终只放当前有效版**（文件名固定，版本号写在文件头）。**升版本的唯一条件：有「决策」被新增 / 推翻 / 删除**（完成度变化、加坑、补实测 → **一律就地改**）。旧版按「版本号 ＋ 日期」移入 `..\archive\`。链条：**初稿 → v1 → v2 → ……**<br>⚠️ **AI 不许擅自升版本** —— 判断"够格"了**只提醒用户**，用户点头才动。详见 `..\plan\PROJECT-PLAN.md` §0.2 |
| 辩论过程在哪 | `..\archive\model-fight\`（5 份）。**除非要追溯"某方案为什么被否掉"，否则不用读** |
| 用户是谁 | 应届生求职者。**学习成本与交付时间是一等约束** —— 方案要"他学得会"，不是"理论上最优" |

---

## 1. 工作区架构

### 1.1 顶层布局（工作区根 = `E:\projects\grounded-qa`）

| 路径 | 在 git 里？ | 职责 | 你能动吗 |
|---|---|---|---|
| `repo\` | ✅ 是 | **代码仓库**（应用、前端、测试、部署配置、CI） | ✅ 主要工作区 |
| `..\plan\` | ❌ 否 | 项目计划书（决策与规划） | ✅ 但改版要走 §8 的流程 |
| `..\ops\` | ❌ 否 | 服务器留档 —— **只有 2 个文件**：`server-log.md`（**操作日志**，只追加）+ `server-notes.md`（**记事**：基线快照 / 配置事实） | ✅ 追加，**不覆盖历史** |
| `..\assets\` | ❌ 否 | 不进 git 的资源 —— ⚠️ **只放"公开、大、可重下"的**（现在只剩知识库 A 的公开文档） | ✅ |
| `..\archive\` | ❌ 否 | **已冻结**的旧版 + `model-fight\` 辩论归档 | 🔴 **只增不改** |
| `..\README.md` | ❌ 否 | 给**用户自己**的项目卡片 | ✅ |

> ⚠️ **2026-10-16（v4）起，自造语料与评测题集不在 `..\assets\` 了** —— 它们在
> **`repo\evals\corpus\` ＋ `repo\evals\questions\`**，**进 git**。
> **分界线是「能不能重下」，不是「是不是我造的」**：知识库 A 是官方文档（公开、大、随时能重下）
> ⟹ 进 git 没意义；知识库 B 是**编的**、小、且**整个评测的可复现性全靠它** ⟹ 必须进 git。
> 理由与代价：`..\plan\PROJECT-PLAN.md` §0.4「v3 → v4」。

### 1.2 仓库内部（`repo\`）

```
repo\
├─ app\                  后端
│   ├─ api\              HTTP 路由 / SSE 流式
│   ├─ core\             配置、鉴权、多租户、日志、计量
│   ├─ rag\              解析→切分→嵌入→检索→精排→生成→拒答
│   ├─ channels\         渠道适配层（web / feishu）—— v1.0 就预留
│   ├─ db\               表模型、迁移、仓储层
│   └─ workers\          异步任务（自研 mini worker）
├─ web\                  前端（Vue 3 + TS + Vite）· src\{api,components,router,stores,types,views}
├─ evals\                评测体系（**进 git**）
│   ├─ corpus\           知识库 B：自造的中文语料（虚构公司）
│   └─ questions\        评测题集 ＋ 标准答案
├─ tests\
├─ scripts\
│   ├─ run\              本地启动脚本
│   └─ kb\               知识库入库脚本
├─ deploy\               nginx / compose / 服务器初始化
├─ docs\{architecture.md, evidence\, records\, archive\}
│                        ⚠️ architecture.md = **解释层唯一归属地**（为什么这么做），见 §12 纪律 7
├─ .github\workflows\
├─ Dockerfile
├─ docker-compose.yml         本地开发栈（PG/Redis/api/worker）
├─ docker-compose.prod.yml    生产栈（+ nginx/Prometheus，严格 mem_limit）
├─ alembic.ini                迁移配置（⚠️ 必须保持**纯 ASCII**！见 §10）
├─ migrations\                Alembic 迁移（env.py + versions\）—— 必须随镜像一起打包
├─ pyproject.toml
├─ AGENTS.md             ← 本文件
└─ README.md             ← 给外人看
```

> ⚠️ **生产 compose 放在仓库根，不在 `deploy\`**（与最初设想不同）：
> compose 里的相对路径是按**该文件所在目录**解析的，放根目录才能让
> `./deploy/prometheus/...` 与 `env_file: .env` 在**本地与服务器上解析一致**。
> `deploy\` 仍放 nginx / 初始化脚本 / Prometheus 配置。

### 1.3 运行时形态（服务器上）

单一 `docker compose` 栈，容器全部设 `mem_limit`：

```
nginx(:8080) ──> 自研 API(FastAPI) ──> PostgreSQL+pgvector
                     │                └> Redis（语义缓存 + BLPOP 队列）
                     │                └> 自研 mini worker（解析/切分/向量化）
                     └> Prometheus + node_exporter（+ 自研看板读其 API）
                     └> 外部 API（对话 / 多模态 / embedding / rerank）
```

**总内存预算 ≈ 670 MB / 可用 ≈ 1.2 GiB**（明细见计划书 §2.3）。

### 1.4 一句话数据流

> 访客提问 → nginx → 意图路由 → **混合检索（全文 + 向量 → RRF → API 精排）** →
> 生成（带引用溯源，低置信拒答）→ SSE 流式返回；文档上传走 Redis 队列 → mini worker 离线解析入库。

---

## 2. 硬约束（违反即错）

1. **服务器只有 2 核 2G**，`available` 仅 ≈ 1.1 GiB（瘦身后 ≈ 1.2 GiB）→ **每个服务必须设 `mem_limit`**。
2. **embedding / rerank / 多模态一律走 API**，**不落本地模型**（否则 +0.8~1.5 GiB，必爆）。
3. **服务端不执行任何用户提交的代码 / 命令。** ← 全项目硬红线。
4. **镜像在 CI 构建 → 推 ACR → 服务器只 `pull`。绝不在这台机器上 `docker build`。**
5. **Docker Hub / GHCR 在大陆拉不动**，`FROM` 基础镜像也拉不动 → 走 registry mirror 或 ACR。
6. **锁版本**：镜像、依赖都不用 `latest`。

---

## 3. 技术栈（定案，不要改）

```
Python 3.13 · FastAPI · SQLAlchemy · PostgreSQL 16 + pgvector · Redis
Vue 3 + TypeScript + Vite
LangChain（最小集）· Function Calling · MCP Server（把检索封成 MCP Server）
Docker + Compose · 阿里云 ACR 个人版 · nginx(:8080)
Prometheus + node_exporter + 自研看板 · GitHub Actions
lark-oapi（飞书长连接，自研适配器）
模型全走 API（**不落本地**）：配置按**能力**命名、**不绑厂商**；
当前 对话 = DeepSeek `deepseek-flash`，embedding / rerank = 百炼（详见计划书 §4.2）
```

**异步任务 = 自研 mini worker（Redis `BLPOP`，~40 行），不是 Celery。**

---

## 4. 不做清单（**不要再提议**）

**Celery · LangBot · Open WebUI · Grafana · LangGraph · Milvus/Qdrant/Chroma/FAISS · Elasticsearch ·
K8s · HTTPS · 微调/SFT/LoRA · 工单/转人工/坐席 · 完整 RBAC/配额 UI · 跨模态向量检索/音视频 ·
Docker Hub/GHCR · 把项目 #1 部署到公网 · 时间表/排期**

> 📌 **这里只放名字** —— 目的是让 AI **一开口就被拦住**（本文件每次会话自动加载）。
> **每条"为什么否掉 ＋ 什么时候该重新考虑"** → `docs\architecture.md` **§10 不做清单**
> （**理由的唯一归属地**，分「技术选型 / 功能范围 / 已知不完美」三类。<br>⚠️ **故意不写条数** —— 那个数字要在几个文件里手工同步，必然漂移；`architecture.md §10` 自己才是权威）；
> **辩论过程** → `..\archive\model-fight\`。

---

## 5. 开发流程（怎么展开）

### 5.1 日常循环

```
① 本地改代码
② 本地静态检查 + 测试          ← 必须绿了才提交
③ 本地 docker compose up 验证   ← 起 PG/Redis/应用，浏览器点一遍
④ commit（小步、语义化信息）
⑤ push → GitHub Actions：lint → test → 构建镜像 → 推 ACR
⑥ 部署 job：ssh → compose pull → up -d → 轮询 /healthz → 失败自动回滚
⑦ 若动了服务器：**命令与结果**追加到 ..\ops\server-log.md（只追加，**不许新建 md**）；
   **结论性事实**（配置 / 端口 / 判据）写 ..\ops\server-notes.md —— 分工见 §12 纪律 6
```

### 5.2 部署前检查清单

- [ ] 依赖 / 镜像**无 `latest`**
- [ ] `.env` **不在仓库里**（`.env.example` 可以）
- [ ] 每个服务有 **`mem_limit` + `healthcheck` + `restart: unless-stopped`**
- [ ] 镜像已推 **ACR**
- [ ] ⚠️ **服务器能拉到的源里有**全部**镜像** —— 不只自建的两个，**公共基础镜像（`pgvector` / `redis` / `prometheus` / `node-exporter`）也要进 ACR**。见 §10 最后一条：**`config` 过 ≠ `up` 过**
- [ ] 数据库迁移脚本已就位
- [ ] `/healthz` 与 `/metrics` 可访问

### 5.3 需要"改服务器"时

先按 §9.2 的权限分级报批，**并准备好回滚命令**。

---

## 6. 构建 / 测试 / 运行命令

> ✅ **骨架已落地**（2026-10-14）。下面是**实际可跑**的命令（已逐条跑绿）。

### 首次准备

```powershell
Copy-Item .env.example .env      # 然后填自己的 API Key（.env 已被 gitignore）
```

### 本地后端（Windows / PowerShell）

```powershell
uv sync --all-groups                       # 安装依赖（含 dev 组）
uv run ruff check .                        # lint
uv run ruff format --check .               # 格式
uv run pytest -q -rs                       # 测试（-rs 会列出"跳过原因"，别省）
uv run uvicorn app.main:app --reload       # 本地起后端
docker compose up -d                       # 起 PG/Redis/api/worker
docker compose logs -f api                 # 看日志
```

### 数据库迁移 / 初始化（**改了模型就必须做**）

```powershell
# ① 生成迁移（对着**真实** PG 比对，所以本地栈要先起来）
uv run alembic revision --autogenerate -m "描述这次改了什么"
# ② 人工过一遍生成的迁移文件！autogenerate 有两件事**管不了**，必须手补：
#    · 用到 pgvector → 顶部 import pgvector.sqlalchemy，且 upgrade() 里要有
#      CREATE EXTENSION IF NOT EXISTS vector（必须在建 chunks 表**之前**）
#    · ⚠️ 顺手 `uv run ruff format <那个文件>` —— autogenerate 产出的是**单引号**，
#      过不了 CI 的 `ruff format --check`（2026-10-16 首次推送前发现，
#      上一次的迁移就因此躺了一颗 CI 必红的雷）
# ③ 应用 + 校验（`check` 报 "No new upgrade operations detected" 才算模型与库一致）
uv run alembic upgrade head
uv run alembic check
# ④ 建默认租户 + 管理员（幂等，可重复跑）
uv run python -m app.db.seed
```

> ⚠️ **宿主机上跑这些命令不需要改端口**：`config.py` 会判断 host 是不是回环地址 ——
> 是（Windows 开发）就走 `POSTGRES_HOST_PORT`（55432），不是（容器内 `postgres`）就走 5432。
> 详见 §10「容器内 5432 / 宿主机 55432」那条。

### 本地前端

```powershell
cd web
pnpm install
pnpm dev                                   # http://localhost:5173 ← 注意是 localhost，不是 127.0.0.1（见 §10）
pnpm build                                 # vue-tsc 类型检查 + 构建
```

### 本地（WSL / bash，如需）

```bash
uv sync --all-groups && uv run pytest -q
docker compose up -d
```

### 服务器（一律通过 `ssh aliyun` 别名）

```bash
ssh aliyun 'cd /opt/grounded-qa && docker compose pull && docker compose up -d'
ssh aliyun 'curl -s localhost:8080/healthz'
ssh aliyun 'docker stats --no-stream'          # 看内存
ssh aliyun 'docker compose logs --tail=100 api'
ssh aliyun 'tmux attach -t grounded'           # 长任务旁观
```

### 知识库入库（✅ 已有离线脚本 `scripts\kb\ingest.py`）

```powershell
# ① 先看切分效果 —— 不连库、不调模型、不花钱。**调切分参数时先跑这个**
uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi --dry-run

# ② 真入库（需要 EMBEDDING_API_KEY）
uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi

# ③ 还没有 embedding key？先只落文本 —— 全文检索路（pg_trgm）不需要向量，照样能用
uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi --no-embed
```

> ⚠️ **`--no-embed` 入库的 chunk 没有向量，补向量只能加 `--replace` 重跑** ——
> 不加的话文档被判成"已入库过"直接跳过，**向量永远补不上，而且不报错**。
> 取语料的清单固化在脚本末尾的 `CORPUS_FILES`；设计取舍见 `architecture.md` §9.18。

---

## 7. 代码约定

### 7.1 ⚠️ 项目级约定（**先看这里**）

> 这些**不是代码风格，是已经定下的决策与陷阱**。它们和"还没开始写代码"无关，现在就必须遵守。

| # | 约定 | 为什么（不看会踩坑） |
|---|---|---|
| 1 | **知识库 A 得分高 ≠ 项目效果好。** 成绩看**知识库 B**（自造语料） | A 库是公开文档，**模型本来就背过**，检索到没检索到都能答对 → 拿 A 库分数当成绩 = 自欺，且面试会被戳穿 |
| 2 | **不要移植项目 #1 的 163 条断言。** 看着是现成资产，**实际是纯返工** | 那些断言是给**代码 agent** 写的（文件操作/shell/工具选择），与 RAG 客服无关 → **只迁方法论，新建 60~100 题** |
| 3 | **不要给 embedding 换模型**，除非准备**全量重嵌入** | 换了**不报错**，只是检索质量**静默变差**，badcase 都归因不出来。库里有 `embedding_model`/`embedding_dim` 校验 |
| 4 | **"不做 HTTPS"是决定，不是遗漏。** 不要"顺手补上" | 用户明确决定；且飞书走**长连接**（出站），本就不需要证书和公网回调 |
| 5 | **内存预算只列容器，不要再加"系统占用"一行** | `available`（1.1 GiB）**已经把系统占用的 492 MB 排除在外** → 加进来就是双重计数（这个坑我们踩过） |
| 6 | **本机是 Windows、服务器是 Linux**，路径 / 换行 / 脚本都不同 | 项目 #1 在这踩过：CRLF 让 CI 变红；PowerShell 写出的 `.env` 带 BOM 导致解析失败 → **写文件一律 UTF-8 无 BOM** |
| 7 | **异步任务用的不是 Celery**，是 ~40 行 `BLPOP` 循环 | 看到没有 Celery **不要以为是漏了**；单机低频批处理场景，重型框架不划算 |
| 8 | **`ops\` / `plan\` / `assets\` / `archive\` 在仓库外**，不是漏提交 | 它们**故意不进 git**（含私有数据 / 内部记录）。<br>⚠️ **但"自造语料 / 评测题集"不在其列**（v4 起）：它们在 **`repo\evals\`**，**要进 git**。**分界线是「能不能重下」** —— 公开文档能重下 ⟹ 不进；自造语料重下不了、且复现全靠它 ⟹ 必须进。见 §1.1 |
| 9 | **测试里不许真调 LLM API** | 烧钱 + 结果不稳定 + 评测失真 → 一律 **mock / 回放 / 缓存** |
| 10 | **知识库 A 的既有结论是"RAG 增益很小"**，这是**诚实结论，不要粉饰** | 增益集中在**私有 / 长尾 / 版本敏感**的知识上 —— 这正是企业做 RAG 的真实动因 |
| 11 | **所有可调项走配置 / 环境变量，不硬编码** | 配额、阈值、模型名、chunk 大小都会变；写死了就要改代码 |
| 12 | **多租户查询一律走 `TenantScopedRepository`；禁止 `session.get(Model, id)`** | 仓储层把 `WHERE tenant_id = ?` **焊进查询构造** —— 你**没有"忘记加条件"这个选项**；写入侧 `repo.build()` 传 `tenant_id` 直接报错。→ **完整理由与正反例**：`architecture.md` §2.2 |
| 13 | **隔离是两层的：租户之间 ＋ 租户内部按用户** | 只做租户隔离的话，同租户的普通成员能互相看到会话；管理员是**租户内的**管理员，**不是**跨租户超管。→ `architecture.md` §2.4 |
| 14 | **越权一律 404，不要 403** | 403 等于承认"这条数据存在" → 可被用来**枚举**有效 id；只有"已登录但角色不够"才给 403。→ `architecture.md` §2.5 |
| 15 | **枚举列用 `pg_enum()` + `enum_check()`，别用 PG 原生 ENUM、也别开 `Enum(create_constraint=True)`** | 原生 ENUM 加取值要 `ALTER TYPE`（老 PG 不能在事务里跑）；`create_constraint=True` 会让 autogenerate **生成两条重复 CHECK**。理由详见 `app\db\base.py` |
| 16 | **测试不许共享全局数据库引擎，每个用例自建一个** | pytest-asyncio **每个测试一个新事件循环**，而 asyncpg 连接**绑定事件循环** → 复用连接池会撞上 `attached to a different loop`。见 `tests\conftest.py` |
| 17 | **拒答必须由「调模型之前」的确定性代码决定，不许问模型** | 让模型自己判断 → 拒答率变成模型心情的副产品，**统计不出来也优化不了**；而且拒答还要付 token。→ `architecture.md` §5.2 |
| 18 | **置信度阈值只能挂在 rerank 分数或向量余弦上，绝不能用 RRF 分数** | RRF 是**名次**的函数：库里只有一条不相关内容时它照样排第一 → 拿它比阈值 ＝ **永远不拒答**。→ `architecture.md` §5.3 |
| 19 | **模型客户端一律「构造时注入」，不许在业务代码里 new** | 不用厂商 SDK（换供应商只改 `.env`）＋ **测试必须能塞假实现**（约定 9）。入口：`pipeline.build_clients()` / `chat.get_clients`。→ `architecture.md` §9.2–9.3 |
| 20 | **相关度 relevance 与 有效性 eligibility 是两件事，不许混进同一个分数** | 相关性是**分数**（向量/精排，看"回答了没有"）；有效性是**布尔**（`documents.content_status` ＋ 生效区间，看"这份文档在**参照时刻**有没有资格被引用"）。**时间永远不进相关性分数** —— 混进去的后果是"越新越相关"，且 badcase 归因不出来。⚠️ 判据是「**在 `ref_time` 是否有效**」（`content_status IN ('active','superseded')` ＋ **闭区间**含 `ref_time`），**不是**「是不是最新」—— 老订单的正确答案就是当年那版。过滤必须写进**召回 SQL 的 `WHERE`**（两条路都要加），**不许召回后再筛**（top-k 已定死，第 21 名永远补不上）。📌 现行 0.980 vs 废止 0.979 —— **相似度在结构上就分不开版本**，别指望换模型解决。→ `architecture.md` §9.36、`..\plan\PROJECT-PLAN.md` §8.6 |

### 7.2 通用风格 —— ✅ **已落地**

> 2026-10-14 起本仓库**已经有代码**，下面是**实际生效**的配置。
> **权威来源仍是 `pyproject.toml` / 前端配置文件**，本表只用来提醒"别自己另发明一套"。

- 格式 / lint：**ruff**（项目 #1 用 `line-length = 100`，`E501` 交给 formatter）
- 测试：**pytest**（项目 #1 用 `asyncio_mode = "auto"`）
- 类型：Python 带 type hints；前端 TypeScript
- 提交：小步、语义化前缀（`feat:` / `fix:` / `docs:` / `chore:`）
- 文档：每份文档开头一行 `状态：当前有效 ｜ 最后更新 YYYY-MM-DD`

---

## 8. 禁区

### 8.1 文件禁区

| 路径 | 规矩 |
|---|---|
| `..\archive\**` | 🔴 **只增不改**。尤其 `model-fight\` —— **绝不修改、绝不删除** |
| `..\ops\server-notes.md` | 「一、基线快照」那几节是**基线**：新快照**新开一节**，旧的原样留着，**不要覆盖** |
| `.env` / `*.key` / `*.pem` / `*credential*` | 🔴 **不读内容、不打印、不进 git**。只允许动 `.env.example` |
| `..\plan\PROJECT-PLAN.md` | **当前有效版**（版本号写在文件头，文件名固定不变）。**只有「有决策被新增 / 推翻 / 删除」才升版本**（按「版本号 ＋ 日期」移入 `..\archive\`）；其余一律**就地改**。**AI 不许擅自升，只提醒用户**。`..\archive\**` 只增不改 |
| `web\dist\`（构建产物） | ❌ 不手改，由构建生成 |
| `.idea\` / `.vscode\` | 进 `.gitignore`，**不提交** |
| `..\assets\**` | 大文件 / 私有数据，**不进 git** |

### 8.2 命令禁区（**别跑**）

| 命令 | 为什么 |
|---|---|
| `docker compose down -v` | **会删数据卷** → 数据库全没 |
| 服务器上 `docker build` / `compose build` | 2C2G **必 OOM**（镜像在 CI 构建） |
| `apt upgrade` / `dist-upgrade` / `do-release-upgrade` | 可能升级内核 → 重启 / 服务中断；安全更新由 `unattended-upgrades` 管 |
| 动 `sshd`（disable/stop/改配置）、改防火墙 / 安全组 | **SSH 是唯一管理通道**，断了就失联 |
| `rm -rf <任何路径>` | 一律**逐条确认**，不许自动执行 |
| 服务器上跑 vLLM / Ollama / 训练 | **无 GPU**，且违反"模型全走 API" |
| 用 `latest` 标签拉镜像 / 装依赖 | 不可复现 |
| 在仓库里 `git add .env` | 历史里一旦进了真 key，清理很痛（项目 #1 扫出过 2 个真 key） |

---

## 9. 环境的特殊要求

### 9.1 两个环境要分清

| | 本地（开发） | 服务器（运行） |
|---|---|---|
| 系统 | **Windows + PowerShell** | **Ubuntu 24.04**（root） |
| 角色 | 写代码、跑测试、本地 compose 验证 | 跑生产栈 |
| 怎么连 | — | **本机已配 `ssh aliyun` 免密别名** |
| `.env` | **独立一份** | **独立一份**（绝不混用 / 绝不同步） |
| GPU | 无 | **无** |

**其他环境要求**：
- 本地依赖服务（PostgreSQL / Redis）**用 docker compose 起，不要装到 Windows 宿主机**。
- **本机端口必须与项目 #1 错开**：本机还跑着 `E:\projects\langgraph-mcp-code-agent`，
  它的依赖栈占了 `3307`（MySQL）/ `6379`（Redis）/ `8888`（SearXNG）/ `80`（nginx）。
  本项目**宿主机**端口用 **PostgreSQL 55432 / Redis 56379 / API 58000**（容器内仍是标准端口）。
  详见 `.env.example` 的「本机端口规划」。容器名统一 `gq-` 前缀、卷统一 `gq-*` 显式命名，**绝不复用项目 #1 的容器/网络/卷**。
- 服务器上任何**超过几十秒**的操作，**必须放进 tmux 会话 `grounded`**（防断线 + 用户可旁观）。
- 写文本文件一律 **UTF-8 无 BOM**；`.gitattributes` 里固定 `text eol=lf` 避免 CRLF 事故。

### 9.2 权限分级（操作服务器时**必须**遵守）

| 级别 | 范围 | 做法 |
|---|---|---|
| 🟢 **只读诊断** | `free` / `ps` / `ss` / `docker ps` / 看日志 | **可直接执行**，但执行前说明将跑什么 |
| 🟡 **改动类** | 装软件 / 改配置 / 启停服务 | **先公示完整命令，用户同意后执行** |
| 🔴 **破坏性** | 删数据/容器/库、改防火墙、动 `sshd` | **逐条确认，绝不自动做** |

**所有服务器操作追加到 `..\ops\server-log.md`**（时间 + 命令 + 结果摘要；报错记完整输出）。
**结论性事实**（"这台机器是这样"）写 `..\ops\server-notes.md` —— **两者别混**（§12 纪律 6）。

---

## 10. 已知坑

> 边做边加。每条写清 **现象 → 原因 → 解法**。

| 现象 | 原因 | 解法 |
|---|---|---|
| CI 里 `FROM python:...` 拉取超时 | 大陆直连 Docker Hub 不可用 | daemon registry mirror / 常用基础镜像推进 ACR |
| **（已被实测推翻）原以为 `:80` 会被阻断** | 大陆未备案服务器 80/443 **传闻**会被阻断 | **2026-10-14 实测：80 完全可用（TCP 通 + HTTP 200）**；反而是 **8080 没放行**（轻量的**「防火墙」**里默认只有 80/443/22/ICMP）。→ 继续用 8080 是"计划已定稿"的取舍，**不是"80 不可用"**；要用 8080 得自己去「防火墙」加一条。详见 `plan\PROJECT-PLAN.md` §2.1.1 |
| **在「云服务器 ECS」控制台里找不到自己的实例 / 安全组列表是空的** | 这台机器是**轻量应用服务器（SWAS）**，不是标准 ECS。**两者是不同产品、不同控制台**（轻量在 `swas.console.aliyun.com`），且轻量**根本没有「安全组」概念**——它的端口叫**「防火墙」**。<br>⚠️ **DMI 判断不了**：轻量跑在 ECS 基础设施上，`/sys/class/dmi/id/product_name` 照样是 `Alibaba Cloud ECS`（我用这个判断错过了，2026-10-14） | 直接看**控制台**：`https://swas.console.aliyun.com/` → 服务器 → 选实例 → **「防火墙」**标签页 → 「添加规则」。**控制台是权威，硬件指纹不是**。<br>📌 **顺带**：轻量实例身上挂着**两个不同的 ID** —— ① 控制台显示的（32 位十六进制、**无前缀**）② 元数据服务 `curl http://100.100.100.200/latest/meta-data/instance-id` 报的（**`i-` 前缀**，机器自报，脚本里用）。**两者不是一回事，别互相"纠正"。** |
| **轻量应用服务器的默认防火墙规则** | 新建实例默认开 **TCP 80 / 443 / 22 + ICMP** 四条 | 这**正好解释**了实测结果：22 通、**80 通**、**8080 不通**（默认规则里没有 8080）。要用 8080 → 自己去「防火墙」加一条 |
| **"我电脑上 80 是开的" / 在服务器上 `curl localhost:80` 测端口放行** | 端口是**服务端**的事：客户端连出去用临时高位端口，**不需要"开"任何端口**；服务器上 `curl localhost` 走**本地回环，根本不经过云平台那层** | 测端口放行**必须从外部发起**（本机 → 服务器公网 IP）。判据：**`REFUSED`** = 已放行但没服务在听；**`TIMEOUT`** = 被丢弃。<br>（云平台那层：**轻量叫「防火墙」，ECS 叫「安全组」**） |
| 检索质量悄悄变差、不报错 | 换过 embedding 模型，新旧向量混用 | `embedding_model`/`embedding_dim` 校验，不一致就拒绝并提示重嵌入 |
| 整机卡死、SSH 失联 | 容器把内存吃光，OOM 杀了 sshd | 全服务 `mem_limit` + 给 PG/sshd 设 `oom_score_adj` |
| 服务在"换页"、响应忽快忽慢 | `vm.swappiness` 默认 60，内核过早换出 anon 页 | 已设 `vm.swappiness=10~20` |
| PowerShell 写的 `.env` 读不出来 | 带 BOM | 一律 UTF-8 **无 BOM** |
| **写进 `/etc/sysctl.d/` 的参数不生效，且不报错** | `/etc/sysctl.d/99-sysctl.conf` 是**指向 `/etc/sysctl.conf` 的软链**，按**字典序**排在 `99-*.conf` 之后 → 同名 key 被它覆盖 | 新文件名必须排在它**后面**：用 **`999-grounded.conf`**（`'-'` < `'9'`，故 `99-sysctl.conf` 在前） |
| 改了 `/etc/sysctl.conf` 第 1 行发现没效果 | 同一文件里 `vm.swappiness` **被定义两次**（第 1 行 `= 0`，第 20 行 `= 10`），**后出现的生效** | 别改 `/etc/sysctl.conf`（阿里云自带文件）；统一写 `999-grounded.conf`，它是最后一道 |
| **停掉 `tuned` 后参数悄悄变了（`vm.swappiness` 变回 0）** | `tuned` 停服时会**主动回滚**它设过的值（设计使然），写回它记录的旧值 `0`，**不是** `/etc/sysctl.conf` 里的 `10` | **先**用 `999-grounded.conf` 固化 `vm.swappiness=10`，**再**停 `tuned`；顺序反了等于没做（`swappiness=0` 让内核极力避免换页 → **OOM 风险反而更高**） |
| **`systemctl disable --now multipathd` 之后它还会被拉起来** | `multipathd` 是 **socket 激活**的：`.service` 和 `.socket` 是两个独立单元。只关 service 时 systemd 只给一句警告 `triggering units are still active`，很容易看漏 | 连 socket 一起关：`systemctl disable --now multipathd.service multipathd.socket` |
| **udev 规则文件明明在，参数却没设上** | `ATTR{...}=4096` **不带引号**时，`udevadm verify` 报 `Invalid key/value pair, ignoring` → 规则被**静默丢弃** | 值**必须带引号**：`ATTR{queue/read_ahead_kb}="4096"`。改完必须做两步验证：`udevadm verify` **＋**"强制打回默认值 → `udevadm trigger` → 看是否被拉回" |
| `systemd-sysctl: command not found` | 它不在 `PATH` | 用全路径：`/usr/lib/systemd/systemd-sysctl --cat-config`（查 sysctl 文件生效顺序） |
| **用 `tmux send-keys` 投递长命令，写进文件的内容被吞掉** | 命令里的 `\n`、`\x22` 等转义序列在「PowerShell → Windows argv → ssh → tmux」多层传递中被层层吃掉 | 投递**短行 + heredoc**，别把转义序列当内容载体；写完**必须 `cat` 复核**（2026-10-14 因此把固化文件写成了垃圾 `1`） |
| **`docker pull` 报 `unexpected media type text/html`，层大小 `0B`** | **镜像加速器返回了 HTML 错误页**，不是 Dockerfile / 网络配置的错。本机 daemon 配了 20 个 mirror，排在最前的 `docker.1panelproxy.com` 就是坏的 | 换可用 mirror（**2026-10-14 实测 `docker.m.daocloud.io` 可用**）。**非侵入式做法**：`docker pull docker.m.daocloud.io/library/<镜像>` 再 `docker tag` 回规范名 —— 这样 compose 里保持规范镜像名，不用改全局 daemon.json |
| **Dockerfile 里 `COPY --from=ghcr.io/...` 本地构建必挂** | GHCR 在大陆拉不动（硬约束 §2.5）。uv 的官方推荐写法正是 `COPY --from=ghcr.io/astral-sh/uv:...` | 别用 ghcr；改成 `pip install uv==<版本>`（走 PyPI）。已在 `Dockerfile` 里改掉 |
| **本项目与项目 #1 的容器 / 端口 / 卷撞车** | 两个项目都用 PostgreSQL / Redis 的默认端口；项目 #1 的 `redis-stack-server` 占着 `6379` | 三道隔离：① 容器名加 `gq-` 前缀 ② 卷用 `gq-*` **显式命名**（不用 compose 自动前缀）③ 宿主机端口挪到 5 万段 |
| **容器以退出码 127 无限重启，日志只有 `[FATAL tini] exec uvicorn failed: No such file or directory`** | 多阶段构建里 **venv 建在 A 路径、拷到 B 路径**：venv 的控制台脚本会把解释器**绝对路径烧进 shebang**（`#!/build/.venv/bin/python`），运行镜像里没有 `/build` → exec 报 ENOENT。**报错极具误导性：看着像"uvicorn 没装"，其实装了**。顺带：`ENV PATH="/app/.venv/bin:$PATH"` 那行删了也是 127，报错一模一样 | 两个阶段的 venv 路径必须**完全一致**（都放 `/app/.venv`）。排查手法：`docker run --rm --entrypoint head <镜像> -1 /app/.venv/bin/uvicorn` 直接看 shebang |
| **worker 容器永远 `unhealthy`，但进程明明活着** | `python:3.13-slim` 里**没有 `pgrep` / `ps`**（procps 不在 slim 基础镜像中），healthcheck 里的 `pgrep` 直接失败 | 运行阶段补装 `procps`。**别在宿主机猜，要在镜像里验**：`docker run --rm --entrypoint which <镜像> pgrep` |
| **`http://127.0.0.1:5173` 打不开，但 Vite 明明说已启动** | Vite 默认绑 `localhost`；Windows 上 `localhost` 可能只解析到 IPv6 `::1`，于是 IPv4 的 `127.0.0.1` 被拒（浏览器报 `ERR_CONNECTION_REFUSED`，很容易误判成"服务没起"） | 用 **`http://localhost:5173`**。要让 IPv4 也能访问：`pnpm dev --host 127.0.0.1` |
| **`alembic` 一连库就挂，报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80`** | Alembic 用 **`encoding="locale"`** 读 `alembic.ini`（`alembic/util/compat.py`）。中文 Windows 的 locale 是 **GBK** → 文件里的 **UTF-8 中文注释**直接把它读崩，**在连数据库之前**就失败。⚠️ Linux（C.UTF-8）上同样内容完全正常 ⇒ **只在开发机复现，服务器上永远看不到** | `alembic.ini` **保持纯 ASCII**（连注释都用英文）；另外 `migrations/env.py` 里 `fileConfig(..., encoding="utf-8")` 也显式给了编码。2026-10-14 实测踩中 |
| **`alembic` 报 `Can't locate timezone: Asia/Shanghai`** | Windows **没有系统时区数据库**，`zoneinfo.ZoneInfo("Asia/Shanghai")` 找不到。Linux 一般有 `/usr/share/zoneinfo`，所以又是"只在开发机复现" | 装 **`tzdata`**（已加进主依赖）。纯数据包、体积小，装上让两边行为一致 |
| **在 PowerShell 里跑 python 脚本，中文输出变成 `���Ĳ���` 这种乱码** | 两边编码对不上：**Python 的 stdout 是 `gbk`**（中文 Windows 的 locale 是 cp936），而 **PowerShell 的 `[Console]::OutputEncoding` 是 `utf-8`** → GBK 字节被当 UTF-8 解读 | ✅ **已修复**（2026-10-15，用户同意后执行）：`[Environment]::SetEnvironmentVariable('PYTHONUTF8','1','User')` —— 让 Python 统一输出 UTF-8。**A/B 实测**：`gbk` → 乱码；`utf-8` → 正常。<br>⚠️ **已经跑着的进程看不到它**（环境变量是进程启动时的快照）→ **新终端 / 重启 DSH 后才生效**；在旧会话里跑会打印中文的脚本，先 `$env:PYTHONUTF8='1'`。<br>📌 **别在脚本里改 `print`** —— 这是**环境**不一致，不是脚本的 bug，每个脚本各修一遍永远修不完（理由见 `docs\architecture.md` §9.11） |
| **迁移文件里出现两条一模一样的 CHECK 约束** | `Enum(..., create_constraint=True)` 会把约束**同时**挂在"列类型"和"表约束"两处，Alembic autogenerate 就生成两条（一条叫 `document_status`、一条叫 `ck_documents_document_status`） | 别开 `create_constraint=True`；用 `app\db\base.py` 的 **`enum_check()`** 显式生成（见 §7.1 约定 15） |
| **`alembic --autogenerate` 生成的迁移一跑就 `NameError` 或建表失败** | autogenerate **管不了两件事**：① 向量列会引用 `pgvector.sqlalchemy.vector.VECTOR` 但**不导入** ② **不会**生成 `CREATE EXTENSION vector` | 每次生成迁移都要**人工过一遍**：顶部补 `import pgvector.sqlalchemy`；`upgrade()` 开头补 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`（**必须在建 `chunks` 表之前**）。已写进 §6 的迁移流程 |
| **测试之间互相干扰、报 `Task got Future attached to a different loop`，而单跑每个测试都过** | pytest-asyncio **每个测试函数一个新事件循环**，而 asyncpg 连接**绑定事件循环** —— 共享一个进程级连接池就会跨循环复用连接。这个报错完全看不出和"测试隔离"有关，极易误判成业务代码 bug | 每个测试**自建引擎**且用 **`NullPool`**（不复用连接）。见 `tests\conftest.py` 与 §7.1 约定 16 |
| **`pytest` 跑着跑着不动了，一行输出都没有，CPU 也几乎不动** | 连的是 `::1:55432`：Windows 上 `localhost` **先解析成 IPv6**，而 Docker 只把端口发布在 IPv4 上 → SYN 没人应答，连接卡在 `SynSent` 里做好几轮重传（几分钟），**看起来像死锁**。2026-10-24 实测卡了 10 分钟以上 | ✅ 已修（`config.py::_dsn_host`）：拼 DSN 时把 `localhost` / `::1` **换成字面量 `127.0.0.1`**。📌 排查手法：`Get-NetTCPConnection -OwningProcess <pytest 的 pid>`，看到 `SynSent ::1` 就是它 |
| **容器内连 5432、宿主机要连 55432，改来改去老是连错库** | compose 把容器的 5432 映射到宿主机 55432（避开项目 #1）。两个端口各有各的用途，靠"记得改 `.env`"必然出错 | 让**代码自己判断**：`config.py` 看 `POSTGRES_HOST` 是不是回环地址 —— 是（Windows 开发）用 `POSTGRES_HOST_PORT`（55432），不是（容器内 `postgres` 服务名）用 `POSTGRES_PORT`（5432）。**所以宿主机上跑 alembic / pytest 不用改任何东西** |
| **中文全文检索一条都搜不到，但接口不报错、状态码 200** | `pg_trgm` 按数据库的 **ctype** 决定"什么字符算词"。`ctype = C` 时**中文不算字母** → `show_trgm('退款政策')` 返回**空集** → `word_similarity()` **恒为 0**。于是混合检索的「全文路」永远召回不出任何东西，系统**不报错**，看起来就像"知识库里确实没有" —— **典型的静默降级**（2026-10-14 实测：`C` → 0；`C.UTF-8` → 0.4） | 建库参数用 **`C.UTF-8`**：`POSTGRES_INITDB_ARGS="--encoding=UTF8 --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8"`（**dev 与 prod 必须一致**）。📌 **已有库只能重建**：`DROP DATABASE` → `CREATE DATABASE ... TEMPLATE template0 LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'` → `alembic upgrade head` → `python -m app.db.seed`。⚠️ 应用**启动时会自检并打告警**（`app/db/checks.py`），别把这个告警当噪音 |
| **`word_similarity` 与 `similarity` 混用** | 两者都是 pg_trgm 的，但语义不同：`similarity(a,b)` 是**整体**相似度，拿 10 字提问去比 800 字 chunk 必然很低（分母是长文本）；`word_similarity(query, text)` 衡量"query 有多像 text 里的**某一小段**" | 检索场景**一律用 `word_similarity`**（pg_trgm ≥ 1.6，PG 16 自带）。见 `app/rag/retrieval.py` |
| **给模型 API 的 `base_url` 少写 `/v1`** | 客户端是拼 `{base}/chat/completions` 的。DeepSeek 两种写法都认，所以本地没问题，**换到只认 `/v1` 的服务时才炸** | 配置里**带上 `/v1`**（`LLM_BASE_URL=https://api.deepseek.com/v1`）。已在 `config.py` 与 `.env.example` 里写全 |
| **SSE 本地好使、部署后变成"憋一大坨再一次性出来"** | nginx 默认会**缓冲**上游响应。直连（不经 nginx）时看不出来，只有过网关才复现 —— 很容易误判成前端 bug | 响应头带 **`X-Accel-Buffering: no`**（`app/api/chat.py` 已加），同时 nginx 那边对该 location 关 `proxy_buffering` |
| **重启电脑后四个容器一个都不在，`restart: unless-stopped` 却没把它们拉回来** | `unless-stopped` 的语义是"**除非被人为停过**"。**手动停掉的容器会被记为"人为停止"**（包括在 Docker Desktop 里停掉整个 compose 项目组），Docker 下次启动时**不会**自动拉起 —— **这是设计，不是故障**（2026-10-15 用户因此白等一场，`seed` 报"连接被拒绝"） | `docker compose up -d` 拉回来即可。⚠️ **数据不受影响**：库在命名卷 `gq-pgdata` 里，重启电脑 / 重启 Docker 都不丢。<br>📌 **判断依据**：`docker ps -a` 里容器**存在**但状态是 `Exited` → 是"被停过"；**根本不存在** → 才是被删了（那就要重建） |
| **想用 `EventSource` 消费问答流** | 它**只能发 GET**，而我们的接口是 `POST` + JSON body（把提问塞 URL 有三个问题：长度、**进 access log**、写操作语义不对） | 用 `fetch` + `ReadableStream` **手写按规范切帧**（`web\src\api\sse.ts`）。⚠️ 三个坑：帧会被切开／多字节字符会被切开（`TextDecoder` 必须 `{stream:true}`）／换行是字段分隔符。→ `architecture.md` §9.12 |
| **Vue 3：把流式消息 push 进 `ref([])` 后改它，界面一个字不动，而且不报错** | `ref([])` 的深层响应式是**读取时**才把元素换成 Proxy；攥着 push 进去的**原始对象**改，就绕过了 Proxy | push **之前**先 `reactive()` 包一层 —— 这样手里的引用本身就是 Proxy。→ `architecture.md` §9.13 |
| **Vue 3：`v-if="someComputed"`（裸标识符）**恒为真** —— 点切换按钮没反应，也不报错** | `@vue/compiler-sfc` 把**裸标识符当整个条件**时做**常量折叠**，`v-if` **整段从编译产物里消失**。（同一个标识符写在**表达式**里 —— 如 `x === 'y'` —— 完全正常，所以极难联想） | 写成**函数调用** `someComputed()`。⚠️ 症状和上一条**一模一样**（"数据变了界面不动、不报错"）但根因在**编译期** → **先去看编译产物**。→ `architecture.md` §9.20 |
| **中文输入法下打字，回车把没选完的拼音发出去了** | 输入法激活期间按 Enter 是"**确认候选词**"，浏览器照样派发 `keydown`。**只在中文输入法下复现** | 判 `event.isComposing`（或旧标记 `keyCode === 229`）就直接 return。→ `architecture.md` §9.14 |
| **`compose config` 能过，`compose up -d` 却失败** | **两关查的东西不一样**：`config` 只做插值/语法（所以 `.env` 填全它就绿）；`up` 要真和 docker daemon 打交道 —— 建网络、建卷、**拉镜像**、起容器。2026-10-17 首次部署就死在这：`.env` 全填好、`config` 通过，但 `up` 卡在 `pgvector/pgvector:0.8.0-pg16` —— **服务器那个加速器 `5haml59c.mirror.aliyuncs.com` 没有这个镜像，404** | ① **别把 `config` 绿当成"能跑"**，两关分开验；<br>② ✅ **已修（`cd7f286`）**：`postgres` / `redis` 的 `image:` **改指 ACR**，`deploy.yml` 的 `build-and-push` 加一步「把公共基础镜像补进 ACR（**幂等**：先从 ACR 拉，拉到就跳过）」—— ⚠️ **只 retag 到服务器本地不够**，`compose pull` 照样按 `image:` 原名去 Docker Hub 拉。<br>📌 **`monitoring` profile 的 `prometheus` / `node-exporter` 已于 v1.1 第 0 步补进 ACR**（2026-10-18）。详见 `..\ops\server-notes.md` C8.7 |
| **每次部署都把 `prometheus` / `node-exporter` 干掉，站点一切正常，只是看板空了** | `prometheus` / `node_exporter` 挂在 **`monitoring` profile** 下。不带 `--profile monitoring` 时它们**不在本次操作的启用集合里**，而 `up -d --remove-orphans` 会把"不在集合里的容器"当孤儿处理 —— 日志里通常只有一行 orphans，**不报错** | CD 里 **pull / up / logs / 回滚 四处都带 `--profile monitoring`**（`deploy.yml` 已加）。⚠️ 反过来说：profile 一启用，`compose pull` 就真的会去拉那两个镜像 ⟹ **必须先在 ACR 里备好**（同上面那条坑） |
| **本机 `docker push` 一个 622 MB 镜像，跑了 57 分钟没结完** | 系统代理是开着的（`ProxyEnable=1`，`ProxyServer=127.0.0.1:10808`），**Docker Desktop 跟随系统代理**，大镜像全走代理上传。实测上行只有 **≈590 KB/s**（`Get-NetAdapterStatistics` 采样 WLAN 得出）——⚠️ **`docker push` 非 TTY 下把进度缓冲住了，看起来像"卡死"，其实是慢** | **大镜像不在本机推**：公共基础镜像由 **CI 的 runner** 推进 ACR（runner 到 ACR 直连，334 MB 不到两分钟）。本机只 `tag` 试探。📌 想知道是"慢"还是"卡"，**别猜**：采两次网卡字节数算速率 |
| **`deploy.yml` 里的「轮询 /healthz」那步，在"镜像坏到起不来"时**永远不执行*** | `nginx` 有 `depends_on: api: condition: service_healthy`。api 一 unhealthy，nginx 就永远不满足依赖 → **`compose up -d` 自己等约 99 秒后返回非零**（`dependency failed to start`）→ job 在「拉取新镜像并重启」就红了，后面**迁移 / seed / 轮询 / 记录版本全部 skipped**（2026-10-17 演练实测，与设计假设不同） | **别据此删掉轮询**：它覆盖的是**更窄**的一类 —— 容器都达标、nginx 也起来了、8080 上仍答不上（nginx 配置错 / 端口没绑 / 上游地址错），那类 `up -d` 是**成功的**。<br>📌 副产物：坏的窗口里站点是**连接被拒**（nginx 根本没起）而不是 503。详见 `..\ops\server-notes.md` C8.8 |
| **想演练"回滚"，却去把镜像构建打坏** | 构建失败时 `deploy` job **压根不启动**（`needs: build-and-push`）⟹ 没有 `up -d`、没有失败信号、**回滚那一步也不会跑**，站点原地不动 —— 什么也验不到 | 要验回滚，必须让**坏镜像真的上到服务器**：改一行 `/healthz`（`raise HTTPException(503, ...)`）→ push → 看它红在 `up -d` → `git revert` → push。**坏的东西必须一键 revert 就好** |
| **用 PowerShell 改一个含中文的源码文件，改完中文全变成乱码 / `?`** | `Get-Content` 按**本机代码页**（GBK）读，`Set-Content -Encoding utf8` 按 UTF-8 写 → **双编码**。改完 ruff 会报一堆错，但**看不出根因是编码**（2026-10-18 就这样写坏过 `app/core/limits.py`） | 改中文文件**只用 `edit` / `write` 工具**（或 `python -c` 并显式 `encoding="utf-8"`）。判据：`read` 看回来是 `闄愭祦` 这种就该**整份重写**，不要在坏文件上继续打补丁 |
| **群机器人"发送成功"了，群里却没有消息** | 飞书 / 企微 webhook **被拒绝时也回 HTTP 200**，错误在 body 里（飞书 `{"code":19021,"msg":"sign match fail"}`、企微 `{"errcode":93000,…}`）—— 最常见的触发是 **IP 白名单没放行**、关键词不匹配、或勾了签名校验 | **必须校验业务码**：飞书看 `code`、企微看 `errcode`，非 0 就当失败（**并且不能记账**，否则"没投递"会被记成"已投递"、**永不重推**）。见 `app/core/alerts.py::_check_delivery`、`architecture.md` §9.30 ⑤ |
| **改了 `.env` 的 `ADMIN_PASSWORD`，登录还是说"密码错"** | `python -m app.db.seed` **只在建号时**写密码，用户已存在就**不改**（刻意的：引导脚本不该悄悄改运营密码）。所以改 `.env` 只对"下次建号"生效，对**已有库无效，且不报错**（2026-10-22 用户就这么被挡在门外） | 判据：seed 输出「管理员已存在 …（未改动）」。用**建号时**那个密码登录；真要换密码得手动更新 `users.hashed_password`，或重建库。<br>⚠️ **同族两个坑（2026-10-25 事故）**：① 改完 `.env` 必须 `up -d --force-recreate --no-deps api worker feishu nginx` —— **env 是"创建容器时"注入的**，`restart` / 不带 recreate 的 `up -d` 不重读，只重建 api 会让 worker/feishu 继续 crash-loop；② `.env` 写坏（如 `ADMIN_PASSWORD=` 留空）→ `Settings` 启动校验抛 `ValidationError` → api crash-loop → `depends_on: service_healthy` 把 **nginx 一起拖住** ⟹ **整站连接被拒**（不是 503），且**回滚救不了**（`.env` 不在版本控制里）。排查先 `docker logs gq-api --tail` |
| **某一步"静默没生效"，而且日志里一点痕迹都没有** | 命令末尾挂着 `\|\| echo 跳过` 这类"失败也算成功"的兜底 —— 于是通道本身出问题时**没人知道**。2026-10-19 真踩到：CI 里 `docker exec … /-/reload` 静默失败，正式告警规则一直没生效，而部署照常绿。**2026-10-24 第二种形态**：`run: pytest \| tee log` 在没有 `pipefail` 的 shell 里拿到的是 **`tee` 的退出码** ⟹ 测试红着、CI 一路绿（`test_vision` 就这么红了好几轮没人知道） | **兜底只许用于"这条真的可以没有"**；关键步骤要**可验证**（改完 reload 就**重启容器并探测健康**），失败**让部署红**。📌 判据：**`\|\| 兜底成功` ＋ 看不见的通道 ＝ 静默失效** —— 和本项目其它坑（全文检索 / 向量漂移 / 限额放行）是同一个病。管道的解法是**显式 `shell: bash`**（它才带 `--noprofile --norc -eo pipefail`），别信"默认带 pipefail" |
| **飞书容器 `healthy`、日志说"已启动"，但飞书一直显示连接失败** | `lark-oapi` 的 `ws.Client` **在构造时**就用 `get_event_loop()` 抓住当时的 loop，`start()` 里再 `run_until_complete` 它。若在**已有 loop 在跑**的主线程里构造（`asyncio.run(...)` 里面）、又丢到别的线程去 `start()` → 子线程里抛 `RuntimeError: This event loop is already running`，**而进程不退出**（所以容器照样 healthy） | 让 lark **独占主线程**（它自己建 loop、阻塞在此），我们的异步活另开一条线程（`gq-async`）。判据：日志里必须有 **`Lark connected to wss://…`** 才算连上。见 `app/channels/feishu_runner.py::_serve` 函数头（2026-10-22 实测踩中） |
| **机器人回了，但飞书群里看到的是一堆 `**粗体**` 和 `\|------\|` 源码** | 飞书 **`msg_type=text` 不渲染 markdown** —— 纯文本发什么就显示什么。而模型的答案天生带加粗 / 列表 / 表格 | 发**卡片**（`msg_type=interactive` ＋ `lark_md` 元素），并且先过 `feishu.to_feishu_markdown()`：`lark_md` **不认表格 / `#` 标题 / `---` 分割线**，得降级成列表行 / 加粗 / 空行；⚠️ **来源摘要也要洗**（它取自 chunk 原文，同样带表格）。→ `architecture.md` §9.37 ⑧（2026-10-22 真机联调踩中） |
| **@ 了机器人，它一点反应都没有（日志里连"收到"都没有）** | 两个原因都会这样：① @ 的是 **v1.1 那个 webhook 自定义机器人**（`alert-bot`，**只发不收**，@ 一万次也不理）；② 自建应用机器人**不在群里 / 没发版** → 飞书**根本不会推事件**。改权限或事件订阅后**必须重新发版**才生效 | 群里「添加机器人」搜的是**应用名称**（不是群名）；确认机器人已在群成员里（群里机器人数量 +1）。判据：日志出现 `收到飞书消息 … group=True` 才算事件到了 |
| **列表页/整页一片空白，控制台也不报错** | 本地 `gq-api` 是**旧镜像**（改完代码没重建），接口返回的 JSON **缺新字段**（如 `channel`）→ 前端按字段分组后**每组都是空** ⟹ `v-for` 渲染零条，看起来像"页面坏了" | 判据：直接 `fetch('/api/v1/xxx')` 看响应里有没有那个字段。**本地跑前端调新代码时别用 compose 里的旧镜像**：`uv run uvicorn app.main:app --port 8000` ＋ `$env:VITE_API_TARGET="http://127.0.0.1:8000"; pnpm dev`（2026-10-25 P2 踩中） |
| **侧栏抽屉浮出来只显示一半 / 整个看不见** | `.app` 上有 `overflow-x: auto`（"内容区最小宽 560"的兜底），**它会裁掉溢出的绝对定位子元素** | 抽屉面板用 **`position: fixed`**（`fixed` 不受祖先 `overflow` 裁剪，除非祖先建了 transform/filter 包含块） |
| **改 `.vue` 里一处样式，"另一个断点"下没变** | 布局断点有**两套判定**：宽度在 `main.css` 的 `--sidebar-w` 媒体查询里，折叠/抽屉状态在 `AppSidebar.vue` 的两个 `matchMedia` 里 | 断点数字**两处一起改**，否则会出现"图标没了、宽度还是 240"的错位。见 `architecture.md` §9.45 ② |
| **容器 `Up (healthy)`，但 `docker logs` **一行都没有**，飞书始终不回** | `mem_limit` 给小了 —— feishu 进程 **anon ≈ 197 MiB**（lark ＋ sqlalchemy ＋ asyncpg ＋ httpx），96m 上限下 **`import lark_oapi` 就被内核 SIGKILL（exit 137）**，**死在打第一行日志之前**；而 healthcheck 只 `pgrep` 进程，重启间隙也能过 → 「健康」和「什么都没干」长得一模一样 | 判据不是报错，是**占用率**：`docker stats` 显示 **≈99.98% / 上限** 就是它。已把 dev＋prod 的 feishu `mem_limit` 抬到 **320m**（`PLAN §2.3` 同步）。⚙️ 记住这条推广形式：**`mem_limit` 顶格 = 静默崩溃，日志是空的**（2026-10-22 实测） |

---

## 11. 文档职责（互不重叠）

| 文件 | 给谁看 | 写什么 |
|---|---|---|
| `repo\README.md` | **外人**（GitHub 访客 / 面试官） | 项目是干什么的、解决什么问题、**怎么安装 / 快速上手（可复制的命令）**、用法示例、截图、依赖、许可、贡献方式、联系方式 |
| `repo\docs\architecture.md` | **人**（面试官 · 三个月后的你自己） | **"为什么这么做"** —— 每个不显然的设计决定：解决什么问题 / 天真做法会怎样 / 代价是什么。**解释层的唯一归属地**（见 §12 纪律 7） |
| `repo\AGENTS.md`（本文件） | **AI 助手** | 流程 / 命令 / 代码风格与约定（尤其反直觉的）/ 禁区 / 环境要求 / 工作区架构 / 当前状态 |
| `..\plan\PROJECT-PLAN.md` | **人 + AI** | 项目计划书（**细节的唯一归属地**）：定位、约束、技术栈、架构、**§8 功能分期与进度（含待定决策 §8.4 / 未验证清单 §8.5）**、缺口、下一步 |
| `..\plan\PHASES.md` | **人 + AI** | **大方向**：只有一张阶段总览表（哪个阶段 / 还剩几个）。**刻意保持薄** |
| `..\plan\CANDIDATES.md` | **人 + AI** | **候选池**：还没定的想法 / 备选方案。**只增不删**；写在这里 ≠ 要做 |
| `..\ops\server-log.md` | 人 + AI | **服务器操作日志**：按时间追加**命令 + 结果**。**只追加，会越写越长** |
| `..\ops\server-notes.md` | 人 + AI | **服务器记事**（**与时间无关的结论性事实**，就地更新）：<br>一、基线快照（动手前 / 瘦身后）｜二、常备备忘 B1–B4（防火墙 / 端口可达性判据 / 镜像加速器 / 瘦身与回滚） |

**规则**：不冗余、不写过程账、不写叙事；`archive\` 只增不改；`plan\` 只保留当前有效版。
**`ops\` 只保留这 2 个文件**（见 §12 纪律 6）。
**解释层的唯一归属地是 `repo\docs\architecture.md`**（见 §12 纪律 7）：**详细解释写那里，本文件 §7.1 只放一句话结论 + 指过去**。
**`plan\` 内部分工**：**`PHASES.md` 放"大方向"（薄）、`PROJECT-PLAN.md` 放"细节"（厚）、`CANDIDATES.md` 放"没定的想法"（只增不删）**。

---

## 12. 工作纪律

| # | 规则 |
|---|---|
| **1** | **能自己定的直接定，不问用户。** 只有「改用户约束」和「动服务器」两类才需要问 |
| **2** | **真要问，一次问清，只问一件。** 不许把决策拆成一串小问题慢慢挤 |
| **3** | **要新增工作项，必须同时砍掉一个 —— 净增为零。** 答不出"换掉哪个"，就不许加 |
| **4** | **想法不进计划书。** 还没定的一律放 `..\plan\CANDIDATES.md`（**只增不删**）；计划书写"已定"，候选池写"没定" |
| **5** | **本文件（AGENTS.md）超过 65 KB 必须压缩。** 助手侧对**指令文件**有 **65 KB 预算**，超了会**从尾部静默截断** ⇒ **后面的内容等于不存在**（拿项目 #1 的实测教训）。<br>→ **加内容前先看体积**：`(Get-Item repo\AGENTS.md).Length / 1KB`。当前约 **61 KB**（2026-10-18，**只剩 ~4 KB 余量**）。快到线时优先把细节**外移**：**"为什么"进 `docs\architecture.md`**（见纪律 7）；**本文件只留"怎么做事 ＋ 一句话结论"**。⚠️ **§10「已知坑」是最大的一块（14 KB，占 1/3）** —— 加坑时解法压到 1~2 句，**长解释放 architecture** |
| **6** | **服务器的事分两个文件记，别混。**<br>· `..\ops\server-log.md` = **操作日志**（跑过什么命令 + 结果）—— **只追加，会越写越长**<br>· `..\ops\server-notes.md` = **记事**（基线快照 / 防火墙 / 端口判据 / 镜像加速器 / 瘦身等**结论性事实**）—— 就地更新<br>**判断标准**：这条是"**我跑了 X**"还是"**这台机器是这样**"？<br>⚠️ **2026-10-14 用户纠正**：我一开始把两者塞进同一个文件，理由是"少几个文件好找" —— **错的**。日志只增不减，把事实埋进流水里，等要找时就得翻几百行。**`ops\` 就这 2 个文件，不许再新开 md。** |
| **7** | **解释必须落 `repo\docs\architecture.md`，不许只留在对话里。**<br>凡是"**为什么这么做 / 换成别的会怎样 / 代价是什么**"这类**解释性内容**，讲完就写进去。<br>**判断方法**：如果你说了"这里有个坑""为什么不这样""我认为最值得说的是……"，那它就该进 `architecture.md`。<br>⚠️ **2026-10-15 用户指出**：我把三条关键设计只在对话里讲了 —— 用户看完才发现"**连我自己都不知道你是这样做的**"。**对话流不是文档**，它会被压缩、会丢失，而且用户**没法回头查**。<br>📌 分工：**详细解释进 `architecture.md`；`AGENTS §7.1` 只放一句话结论 + 指过去**（避免两边各写一份然后慢慢跑偏） |
| **8** | **文档两条路径：同步「持续小步」，重整「症状触发」——绝不用"上下文快满"当触发。**<br>· **A 持续小步（默认）**：每完成一件事，立刻把 delta 写进**已有**位置。**不接受"先记着，回头一起写"。**<br>· **B 症状触发重整（例外）**：只有出现 ① 找不到东西 ② 同一事实两处且开始不一致 ③ 某文件超体积预算 ④ 某文件过半内容已"过期" —— 才动**结构**。<br>⚠️ **为什么不用"上下文快满"**：那正是**最没空间**做细致重整的时刻；而且它把"同步"和"重整"混成一件事 → 会攒成一次性大扫除；结构频繁变动还会让你**记不住东西在哪**。<br>✅ **上下文快满时**：什么都不用特别做（状态本来就该在盘上）。最多核对 `§13` / `PLAN §8.2` / `PHASES` 三个数字 —— 5 分钟，**不是重整** |

**对话流不作为正式记录**（上下文压缩会丢信息，且可能换会话 / 换 agent）→ 该落文件的一律落文件。

---

## 13. 当前状态

| 项 | 值 |
|---|---|
| 阶段 | **v1.2 ✅ 收口（3/3）**、**v1.3 ✅ 收口** —— 阶段 0 / 1（v1.0 12/12）/ 2（v1.1 5/5）已 ✅ 收官。v1.2：闭卷/开卷对照 ✅、**提问带图**（A 线索型，后端＋前端＋评测）✅、飞书渠道**本地＋生产都打通**（卡片回复）✅、小补丁 ✅。<br>**阶段 4 · v1.3（知识库治理）全部 ✅ 2026-10-23** —— A 加列回填、B 召回过滤、**B2 `ref_time` 按参照时刻选版本**、**C 冲突告警**，另加**引用串号治理**。<br>🚀 生产：`http://120.24.67.82:8080`（**跑的就是最新 `main`** —— 每次 push 都会走 CD，所以别在文档里追版本号），8 容器 healthy、DB **36 篇 / 237 块**全带向量、`orders=20`<br>✅ **471 项测试（446 通过 · 25 跳过）**；评测：检索（60 题）全文 0.567 / 向量 0.933 / RRF 0.917 / **精排 0.983（59/60）· MRR 0.885**；生成三组要点命中（2026-10-24 重跑）**28.9% / 10.5% / 94.7%**、读图 **7/7**、带图 **2/3 vs 0/3** |
| 计划书版本 | `plan\PROJECT-PLAN.md` **v10**（当前有效版，版本号写在文件头；**v10 = 2026-10-26**：**阶段 7 · agent 改造开档** —— D6 演示账号＝只读管理员 / D7 手写单循环不引框架 / D8 工具只留 4 个，规格全在 **§13**；v9 = 2026-10-24 的走查阶段内容仍在 **§12**，未改）。历史版本逐字冻结在 `..\archive\`：**初稿** `PROJECT-PLAN-初稿-2026-10-13.md`、**v1** `…-v1-2026-10-15.md`、**v2** `…-v2-2026-10-15.md`、**v3** `…-v3-2026-10-15.md`、**v4** `…-v4-2026-10-16.md`、**v5** `PROJECT-PLAN-v5-2026-10-19.md`（SHA256 `DE308ED1…591A`）、**v6** `PROJECT-PLAN-v6-2026-10-19.md`（SHA256 `0B553511…`）、**v7** `PROJECT-PLAN-v7-2026-10-22.md`（SHA256 `6CF15ED9…`）、**v8** `PROJECT-PLAN-v8-2026-10-24.md`（SHA256 `C9BC953B…`，125 531 B）、**v9** `PROJECT-PLAN-v9-2026-10-24.md`（SHA256 `19BF3A23…E602`，150 959 B）。<br>⚠️ 归档名取**该版自己的定版日** —— 见 `PLAN §0.4`。<br>📌 **v2 起划定边界**：**已完成历史 → `git log`**；执行细节 → `..\ops\`；解释 → `repo\docs\architecture.md` |
| 大方向 | **`plan\PHASES.md`**（薄表）—— **阶段 0 / 1（v1.0）/ 2（v1.1）全部 ✅ 完成**；**阶段 3 · v1.2 ✅ 完成（3/3）**，明细见 **`plan\PROJECT-PLAN.md` §8.3**；**阶段 4 · v1.3 全部 ✅（A ＋ B ＋ B2 ＋ C ＋ 引用治理）**，明细见 **§8.6** |
| 下一步 | 🔄 **阶段 7 · agent 改造（2026-10-26 开档，计划书 v10）** —— **规格唯一归属地＝`..\plan\PROJECT-PLAN.md` §13**：4 个工具（`search_kb` / `get_order` / `ask_user` / `finish`）· **手写单循环 ≤6 步**（**不引 LangChain、不碰 MCP**，理由见 `docs/architecture.md` §9.48）· scratchpad 跨轮 · 并行 tool_calls · 自愈重试 · 交卷前自查 · `ANSWER_MODE=pipeline\|agent`（**默认 pipeline**）· 16+6 题配对评测。**顺序不能颠倒**：agent 跑通 → 降级路径接 scratchpad → 评测 → 最后才删 `followup.py` 与正则抽单号。<br>✅ 阶段 6（走查 ＋ README 重写）2026-10-25 全部收尾，明细 `PLAN §12.3`；演示账号已改成**只读管理员**（D6，`architecture §9.47`）。<br>📷 **截图怎么拍**：`gq-api` 容器里签一个 admin JWT（**不读密码**）＋ 本机注入 `localStorage` 的反向代理（模板 `.tmp/shotproxy.py`，不进 git），拍完删 token。<br>🔧 **六条硬规矩**（P2/P2.7/P2.8 定）：① 导航**不许再回到横排**；② 断点数字在 `main.css` 与 `AppSidebar.vue` **两处**，改一处必须改两处；③ 各页**不再自己写 `<h2>`**，用 `<AppHeader title hint tone back>`；④ 「看原文」抽屉**默认不发请求 / 只取 `ordinal ± 1` / 只开一个**；⑤ **删除一律硬删 + 确认框**（不留回收站），越权一律 404；⑥ **列表一律按组翻页**（全局 `limit` 会让一个组吃光配额）。解释 → `docs/architecture.md` §9.45–9.46。<br>✅ **飞书真名已真机验过**：群名 `grounded-qa-bot-test`、用户名 `用户627851`；⚠️ 权限名是 **`contact:contact.base:readonly`**（`contact:user.base:readonly` 是旧名，不生效）。<br>⚠️ **不许自己加批次外的活**；⚠️ 演示登录默认关（`.env` 里 `DEMO_LOGIN_ENABLED=false`）。<br>⚠️ **本机 `docker stop gq-feishu`**：本地那个飞书容器会和生产抢同一条长连接（Docker Desktop 一重启它自己就回来了）。 |
| 未决项 | **无**（§8.4 D1 已**实测收口**：`pg_trgm` 在 `ctype=C` 下对中文完全失效，已改 `C.UTF-8`，路径不变） |
| 刚完成 | ✅ **README 重写完毕 ＋ 项目收官（2026-10-25）** —— `repo\README.md` 按走查后的最终能力面重写：能力表 12 行 ＋ 不显然的决定 8 条 ＋ 评测两表与归因 ＋ 治理 / 渠道 / 部署各一节，**347 行 → 194 行**；截图全部换成**生产真机**（`chat` / `context` / `conversations` / `documents` / `monitoring`），`feishu` / `reports` 占位图与旧 `conflicts.png` 删掉。<br>🐞 顺带修：运维页「拒答率样本量」显示成小数（Prometheus `increase()` 按抓取点外推，界面上出现过 `8.0273096922373 次`）→ 前端取整（`761b5d1`）。<br>✅ **v1.3 收尾两项（2026-10-23）** —— **C 冲突告警**：`app/kb/conflicts.py` ＋ CLI ＋ `GET /reports/conflicts`（管理员），判据 = 同主题 ＋ **同适用范围** ＋ 同单位族 ＋ 值不同；首跑 **199 处假冲突 → 收口到 3 处真冲突**。⚠️ 召回率低是已知短板，只抓不改。<br>**引用串号治理**：`app/rag/citations.py` 核"数字＋单位"是否真在被引段落里 → `Answer.extra["citation_audit"]` ＋ `gq_citation_support_total`；**只抓不改**（改写已流式发出的回答有两个版本，误删正确引用更糟）。<br>📎 更早：B2 / 飞书渠道 / 提问带图 → **`git log`**（提交信息里写了"为什么"）。**本文件不再逐条累积** |
