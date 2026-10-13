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
| 运行时在哪 | **阿里云 ECS 2 核 2G**（Ubuntu 24.04），公网 `120.24.67.82` |
| **完整决策在哪** | **`..\plan\PROJECT-PLAN.md`** —— 唯一有效的计划书，**动手前应当读一遍** |
| 辩论过程在哪 | `..\archive\model-fight\`（5 份）。**除非要追溯"某方案为什么被否掉"，否则不用读** |
| 用户是谁 | 应届生求职者。**学习成本与交付时间是一等约束** —— 方案要"他学得会"，不是"理论上最优" |

---

## 1. 工作区架构

### 1.1 顶层布局（工作区根 = `E:\projects\grounded-qa`）

| 路径 | 在 git 里？ | 职责 | 你能动吗 |
|---|---|---|---|
| `repo\` | ✅ 是 | **代码仓库**（应用、前端、测试、部署配置、CI） | ✅ 主要工作区 |
| `..\plan\` | ❌ 否 | 项目计划书（决策与规划） | ✅ 但改版要走 §8 的流程 |
| `..\ops\` | ❌ 否 | 服务器操作留档（快照 + 命令日志） | ✅ 追加，**不覆盖历史** |
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
模型全走 API：对话/多模态=智谱免费档；embedding/rerank=百炼
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
⑦ 若动了服务器，把命令与结果追加到 ..\ops\server-log.md
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
pnpm dev                                   # http://127.0.0.1:5173（已配好 /healthz 等代理）
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

### 7.2 通用风格 —— ⚠️ **尚未落地，仅作参照**

> **本仓库还没有代码，下面是沿用项目 #1 的做法**（不是我们现在"定"的）。
> **落地时以 `pyproject.toml` / 前端配置文件里的实际配置为准**，本表只用来提醒"别自己另发明一套"。

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
| `..\plan\PROJECT-PLAN.md` | 改版走 §8 流程：**旧版带日期移入 `..\archive\`，再写新版**；不要原地大改 |
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
- 服务器上任何**超过几十秒**的操作，**必须放进 tmux 会话 `grounded`**（防断线 + 用户可旁观）。
- 写文本文件一律 **UTF-8 无 BOM**；`.gitattributes` 里固定 `text eol=lf` 避免 CRLF 事故。

### 9.2 权限分级（操作服务器时**必须**遵守）

| 级别 | 范围 | 做法 |
|---|---|---|
| 🟢 **只读诊断** | `free` / `ps` / `ss` / `docker ps` / 看日志 | **可直接执行**，但执行前说明将跑什么 |
| 🟡 **改动类** | 装软件 / 改配置 / 启停服务 | **先公示完整命令，用户同意后执行** |
| 🔴 **破坏性** | 删数据/容器/库、改防火墙、动 `sshd` | **逐条确认，绝不自动做** |

**所有服务器操作追加到 `..\ops\server-log.md`**（时间 + 命令 + 结果摘要；报错记完整输出）。

---

## 10. 已知坑

> 边做边加。每条写清 **现象 → 原因 → 解法**。

| 现象 | 原因 | 解法 |
|---|---|---|
| CI 里 `FROM python:...` 拉取超时 | 大陆直连 Docker Hub 不可用 | daemon registry mirror / 常用基础镜像推进 ACR |
| `:80` 打不开 | 大陆未备案服务器 80/443 可能被阻断 | 服务按 **:8080** 规划；**v1.0 第一天实测** |
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

---

## 11. 文档职责（三份不重叠）

| 文件 | 给谁看 | 写什么 |
|---|---|---|
| `repo\README.md` | **外人**（GitHub 访客 / 面试官） | 项目是干什么的、解决什么问题、**怎么安装 / 快速上手（可复制的命令）**、用法示例、截图、依赖、许可、贡献方式、联系方式 |
| `repo\AGENTS.md`（本文件） | **AI 助手** | 流程 / 命令 / 代码风格与约定（尤其反直觉的）/ 禁区 / 环境要求 / 工作区架构 / 当前状态 |
| `..\plan\PROJECT-PLAN.md` | **人 + AI** | 项目计划书：定位、约束、技术栈、架构、分期、缺口、**现在与后续的完整规划** |
| `..\ops\server-log.md` | 人 + AI | 服务器命令流水 |

**规则**：不冗余、不写过程账、不写叙事；`archive\` 只增不改；`plan\` 只保留当前有效版。

---

## 12. 工作纪律

| # | 规则 |
|---|---|
| **1** | **能自己定的直接定，不问用户。** 只有「改用户约束」和「动服务器」两类才需要问 |
| **2** | **真要问，一次问清，只问一件。** 不许把决策拆成一串小问题慢慢挤 |
| **3** | **要新增工作项，必须同时砍掉一个 —— 净增为零。** 答不出"换掉哪个"，就不许加 |

**对话流不作为正式记录**（上下文压缩会丢信息，且可能换会话 / 换 agent）→ 该落文件的一律落文件。

---

## 13. 当前状态

| 项 | 值 |
|---|---|
| 阶段 | **v1.0 骨架已落地**（可跑通 lint / test / compose / 前端 build），业务功能尚未开始 |
| 计划书版本 | `plan\PROJECT-PLAN.md` **v2（补坑版）**；v1 已逐字冻结在 `..\archive\PROJECT-PLAN-v1-2026-10-13.md` |
| 下一步 | **补 v1.0 业务功能**：① 登录 + `tenant_id` 数据模型 ② 会话列表 + 聊天（引用溯源 + 拒答 + SSE）③ 知识库上传 + 入库 worker ④ 只读评测报告页 |
| 未决项 | **无** |
| 刚完成 | ✅ **系统瘦身**（2026-10-14，关 6 单元，`available` +100 MB，详见 §10）<br>✅ **v1.0 骨架**（2026-10-14，首个提交 `67b8d1d`，44 文件；`ruff`/`pytest`/`compose config`/`pnpm build` 全绿） |
