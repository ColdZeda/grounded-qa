# AGENTS.md · 给 AI 助手的操作手册

> 状态：**当前有效** ｜ 最后更新 2026-10-14

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
| 代码仓库 | `E:\projects\grounded-qa\repo`（git；将来推 GitHub） |
| 运行时在哪 | **阿里云「轻量应用服务器」（SWAS）** 2 核 2G（Ubuntu 24.04），公网 `120.24.67.82`<br>⚠️ **不是标准 ECS** —— 见 §10 那条坑（端口在**「防火墙」**里管，不是「安全组」） |
| **完整决策在哪** | **`..\plan\PROJECT-PLAN.md`** —— 唯一有效的计划书，**细节的唯一归属地**（含 §8 功能分期与进度），**动手前应当读一遍** |
| **大方向在哪** | **`..\plan\PHASES.md`** —— **只有一张阶段总览表**（现在哪个阶段、还剩几个）。**刻意保持薄**；细节一律去 §8 |
| **想法放哪** | **`..\plan\CANDIDATES.md`（候选池）** —— 还没定的点子放这里，**只增不删**。<br>⚠️ **想法绝不写进计划书**，计划书只写"已定"的 |
| 计划书版本规则 | `plan\` 里**始终只放当前有效版**（文件名固定，版本号写在文件头）；**只有大改才推进版本**，旧版按「版本号+日期」移入 `..\archive\`；**小修就地改**。链条：**初稿 → v1 → v2 → ……** |
| 辩论过程在哪 | `..\archive\model-fight\`（5 份）。**除非要追溯"某方案为什么被否掉"，否则不用读** |
| 用户是谁 | 应届生求职者。**学习成本与交付时间是一等约束** —— 方案要"他学得会"，不是"理论上最优" |

---

## 1. 工作区架构

### 1.1 顶层布局（工作区根 = `E:\projects\grounded-qa`）

| 路径 | 在 git 里？ | 职责 | 你能动吗 |
|---|---|---|---|
| `repo\` | ✅ 是 | **代码仓库**（应用、前端、测试、部署配置、CI） | ✅ 主要工作区 |
| `..\plan\` | ❌ 否 | 项目计划书（决策与规划） | ✅ 但改版要走 §8 的流程 |
| `..\ops\` | ❌ 否 | 服务器留档 —— **只有 2 个文件**：`server-log.md`（**唯一记事本**）+ `server-snapshot.md`（改造前基线） | ✅ 追加，**不覆盖历史** |
| `..\assets\` | ❌ 否 | 不进 git 的资源（知识库原文、自造文档、评测题集） | ✅ |
| `..\archive\` | ❌ 否 | **已冻结**的旧版 + `model-fight\` 辩论归档 | 🔴 **只增不改** |
| `..\README.md` | ❌ 否 | 给**用户自己**的项目卡片 | ✅ |

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
├─ web\                  前端（Vue 3 + TypeScript + Vite）
├─ evals\                评测体系（题集 / 断言 / 跑批 / 报告）
├─ tests\
├─ scripts\
│   ├─ run\              本地启动脚本
│   └─ kb\               知识库入库脚本
├─ deploy\               nginx / compose / 服务器初始化
├─ docs\{architecture.md, evidence\, records\, archive\}
├─ .github\workflows\
├─ Dockerfile
├─ docker-compose.yml         本地开发栈（PG/Redis/api/worker）
├─ docker-compose.prod.yml    生产栈（+ nginx/Prometheus，严格 mem_limit）
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

## 4. 已排除（**不要再提议**）

```
Celery · LangBot · Open WebUI · Grafana · LangGraph
Milvus / Qdrant / Chroma / FAISS · Elasticsearch
K8s · HTTPS · 微调 / SFT / LoRA
工单 / 转人工 / 坐席系统 · 完整 RBAC / 配额 UI · 跨模态向量检索 / 音视频
Docker Hub / GHCR · 把项目 #1 部署到公网 · 时间表 / 排期
```

**理由摘要见 `..\archive\model-fight\`。不要在方案里重新引入。**

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
⑦ 若动了服务器，把命令与结果追加到 ..\ops\server-log.md（**服务器唯一的记事文件，不许新建 md** —— §12 纪律 6）
```

### 5.2 部署前检查清单

- [ ] 依赖 / 镜像**无 `latest`**
- [ ] `.env` **不在仓库里**（`.env.example` 可以）
- [ ] 每个服务有 **`mem_limit` + `healthcheck` + `restart: unless-stopped`**
- [ ] 镜像已推 **ACR**
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
uv run pytest -q                           # 测试（不需要 docker）
uv run uvicorn app.main:app --reload       # 本地起后端
docker compose up -d                       # 起 PG/Redis/api/worker
docker compose logs -f api                 # 看日志
```

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

### 知识库入库

```bash
uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi
```

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
| 8 | **`ops\` / `plan\` / `assets\` / `archive\` 在仓库外**，不是漏提交 | 它们**故意不进 git**（含私有数据 / 内部记录） |
| 9 | **测试里不许真调 LLM API** | 烧钱 + 结果不稳定 + 评测失真 → 一律 **mock / 回放 / 缓存** |
| 10 | **知识库 A 的既有结论是"RAG 增益很小"**，这是**诚实结论，不要粉饰** | 增益集中在**私有 / 长尾 / 版本敏感**的知识上 —— 这正是企业做 RAG 的真实动因 |
| 11 | **所有可调项走配置 / 环境变量，不硬编码** | 配额、阈值、模型名、chunk 大小都会变；写死了就要改代码 |

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
| `..\ops\server-snapshot.md` | 初始快照是**基线**；要记改造后状态就**新开一节**，不要覆盖 |
| `.env` / `*.key` / `*.pem` / `*credential*` | 🔴 **不读内容、不打印、不进 git**。只允许动 `.env.example` |
| `..\plan\PROJECT-PLAN.md` | **当前有效版**（版本号写在文件头，文件名固定不变）。**只有大改才推进版本**：按「版本号 + 日期」移入 `..\archive\` 再写新版；**小修就地改**。`..\archive\**` 只增不改 |
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
**这是服务器唯一的记事文件** —— 不许另开新 md（§12 纪律 6）。

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
| **停掉 `tuned` 后参数悄悄变了** | `tuned` 停服时会**主动回滚**它设过的值（这是设计，不是 bug） | 想保留的值必须**先固化**（sysctl.d / udev 规则），**再**停 `tuned`；顺序反了＝等于没固化 |
| **停掉 `tuned` 后 `vm.swappiness` 变成 0** | 同上，但更具体：回滚把它写回记录的旧值 `0`，**不是**你以为的 `/etc/sysctl.conf` 里的 `10` | **先**用 `999-grounded.conf` 固化 `vm.swappiness=10` 再停。`swappiness=0` 会让内核极力避免换页 → **提高 OOM 风险**，对这台机器是**倒退**（2026-10-14 实测踩中） |
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

---

## 11. 文档职责（互不重叠）

| 文件 | 给谁看 | 写什么 |
|---|---|---|
| `repo\README.md` | **外人**（GitHub 访客 / 面试官） | 项目是干什么的、解决什么问题、**怎么安装 / 快速上手（可复制的命令）**、用法示例、截图、依赖、许可、贡献方式、联系方式 |
| `repo\AGENTS.md`（本文件） | **AI 助手** | 流程 / 命令 / 代码风格与约定（尤其反直觉的）/ 禁区 / 环境要求 / 工作区架构 / 当前状态 |
| `..\plan\PROJECT-PLAN.md` | **人 + AI** | 项目计划书（**细节的唯一归属地**）：定位、约束、技术栈、架构、**§8 功能分期与进度（含待定决策 §8.4 / 未验证清单 §8.5）**、缺口、下一步 |
| `..\plan\PHASES.md` | **人 + AI** | **大方向**：只有一张阶段总览表（哪个阶段 / 还剩几个）。**刻意保持薄** |
| `..\plan\CANDIDATES.md` | **人 + AI** | **候选池**：还没定的想法 / 备选方案。**只增不删**；写在这里 ≠ 要做 |
| `..\ops\server-log.md` | 人 + AI | **服务器唯一的记事文件**（2026-10-14 由 3 份合并而成）。<br>**A. 流水日志**：按时间追加命令 + 结果；**B. 常备备忘**：防火墙 / 端口可达性判据 / 镜像加速器 / 瘦身与回滚 |
| `..\ops\server-snapshot.md` | 人 + AI | **改造前的一次性基线快照**（只新开一节，**不覆盖**）。**它不是记事本**，不追加流水 |

**规则**：不冗余、不写过程账、不写叙事；`archive\` 只增不改；`plan\` 只保留当前有效版。
**`ops\` 只保留这 2 个文件**（见 §12 纪律 6）。
**`plan\` 内部分工**：**`PHASES.md` 放"大方向"（薄）、`PROJECT-PLAN.md` 放"细节"（厚）、`CANDIDATES.md` 放"没定的想法"（只增不删）**。

---

## 12. 工作纪律

| # | 规则 |
|---|---|
| **1** | **能自己定的直接定，不问用户。** 只有「改用户约束」和「动服务器」两类才需要问 |
| **2** | **真要问，一次问清，只问一件。** 不许把决策拆成一串小问题慢慢挤 |
| **3** | **要新增工作项，必须同时砍掉一个 —— 净增为零。** 答不出"换掉哪个"，就不许加 |
| **4** | **想法不进计划书。** 还没定的一律放 `..\plan\CANDIDATES.md`（**只增不删**）；计划书写"已定"，候选池写"没定" |
| **5** | **本文件（AGENTS.md）超过 65 KB 必须压缩。** 助手侧对**指令文件**有 **65 KB 预算**，超了会**从尾部静默截断** ⇒ **后面的内容等于不存在**（拿项目 #1 的实测教训）。<br>→ **加内容前先看体积**：`(Get-Item repo\AGENTS.md).Length / 1KB`。当前约 **27 KB**。快到线时优先把细节**外移**到 `plan\PROJECT-PLAN.md`（本文件只留"怎么做事"，不留"项目细节"） |
| **6** | **服务器的事只记一个文件：`..\ops\server-log.md`。** 凡是"服务器上做了什么 / 服务器现在是什么样"，**一律追加到它**（A 流水 / B 备忘）——**不许再新开 md**（2026-10-14 用户要求：原先散着 3 份，找起来费劲）。<br>**唯一例外**：`server-snapshot.md` 是**一次性基线快照**，只新开一节，**不覆盖**，也**不当记事本用** |

**对话流不作为正式记录**（上下文压缩会丢信息，且可能换会话 / 换 agent）→ 该落文件的一律落文件。

---

## 13. 当前状态

| 项 | 值 |
|---|---|
| 阶段 | **v1.0 骨架已落地**（可跑通 lint / test / compose / 前端 build），业务功能尚未开始 |
| 计划书版本 | `plan\PROJECT-PLAN.md` **v1**（当前有效版，版本号写在文件头）；改版前的定稿已命名**初稿**，逐字冻结在 `..\archive\PROJECT-PLAN-初稿-2026-10-13.md` |
| 大方向 | **`plan\PHASES.md`**（薄表）—— 当前在 **阶段 1 · v1.0（约 36%）**；明细见 **`plan\PROJECT-PLAN.md` §8** |
| 下一步 | **补 v1.0 业务功能**：① 登录 + `tenant_id` 数据模型 ② 会话列表 + 聊天（引用溯源 + 拒答 + SSE）③ 知识库上传 + 入库 worker ④ 只读评测报告页 |
| 未决项 | **无** |
| 刚完成 | ✅ **系统瘦身**（2026-10-14，关 6 单元，`available` +100 MB，详见 §10）<br>✅ **v1.0 骨架**（2026-10-14，首个提交 `47f44a8`，44 文件；`ruff`/`pytest`/`compose config`/`pnpm build` 全绿）<br>✅ **本地 compose 实跑通**（2026-10-14，提交 `5464f26`：4 容器 healthy、浏览器点通、修掉 4 个只在真跑时暴露的问题）<br>✅ **git 身份改为 GitHub noreply**（`ColdZeda <214781303+ColdZeda@users.noreply.github.com>`，仓库级；**全部历史提交的作者已一并改写**，所以哈希与改写前不同）<br>✅ **端口实测（结论推翻原假设）**：**80 完全可用（TCP 通 + HTTP 200）**，反而 **8080 没放行**（轻量的「防火墙」默认只开 80/443/22）。详见 §10 与 `plan\PROJECT-PLAN.md` §2.1.1<br>✅ **8080 已放行**（2026-10-14，用户在轻量「防火墙」加 TCP 8080 → 0.0.0.0/0；外网复测 `TIMEOUT` → **`REFUSED`**）<br>✅ **`ops\` 已收敛为 2 个文件**（2026-10-14，用户要求）：`registry-mirror.md` + `slimming-record.md` **并入 `server-log.md`**；今后服务器记录**只写 `server-log.md`**（§11 / §12 纪律 6） |
