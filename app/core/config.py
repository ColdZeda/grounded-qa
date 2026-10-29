"""全局配置：所有可调项走环境变量，不硬编码（AGENTS.md §7.1 约定 11）。

模型全部走外部 API，所以这里只有 base_url / api_key / model 名，没有模型路径（AGENTS 硬约束 §2）。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ⚠️ 两个"开发默认值"抽成常量，方便启动校验直接比对（也避免在多处重复字面量）。
# ⚠️ `DEV_JWT_SECRET` 给足 32 字节 —— 见 Settings.jwt_secret 的说明。
DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-in-production"
DEFAULT_ADMIN_PASSWORD = "change-me"

#: 一键演示账号的邮箱。**只在这三处出现**：`app/api/auth.py`（find-or-create）、
#: `app/api/deps.py`（拦写操作）、`app/db/models/user.py`（`is_demo` 属性）。
#: ⚠️ 刻意用 `demo-visitor@` 而不是 `demo@`：后者容易和"开发者自己建的本地账号"撞上。
DEMO_EMAIL = "demo-visitor@grounded.local"

#: 已实测校准过 `answer_min_score` 的 rerank 模型；判定只看"模型在不在表里"。
#: 重新标定完把新模型名加进来 —— 这是校准记录，不是配置项。
CALIBRATED_RERANK_MODELS: dict[str, str] = {
    "qwen3.7-text-rerank": (
        "2026-10-15 · 20 条样本 · 取 0.60"
        "（相关 0.786~0.990 ／ 无关 0.009~0.466，完全分离，中间空 0.32）"
    ),
}


class Settings(BaseSettings):
    """从环境变量 / `.env` 读取的全局配置。字段名大小写不敏感。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用
    app_version: str = "0.1.0"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # PostgreSQL
    postgres_host: str = "localhost"
    # ⚠️ **容器内互访**端口（容器网络里就是标准的 5432）
    postgres_port: int = 5432
    # ⚠️ **宿主机侧**端口 —— compose 把容器的 5432 映射到宿主机的 55432
    #    （避开项目 #1 的 3306/6379/8888/80，见 .env.example 的「本机端口规划」）。
    #    用途：在 Windows 上直接跑 `uv run alembic ...` / `uv run pytest` 时，
    #    连的必须是**映射出来的** 55432，而不是容器内的 5432。
    #    判断规则见 `database_url`：**只有 host 是回环地址时才用它**。
    #    容器里的 `POSTGRES_HOST` 是服务名 `postgres`（不是回环），所以不受影响。
    postgres_host_port: int = 55432
    postgres_db: str = "grounded_qa"
    postgres_user: str = "grounded"
    postgres_password: str = "change-me"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_maxmemory: str = "96mb"

    # 对话 / 多模态（任意 OpenAI 兼容端点）
    # ⚠️ 刻意**不绑定厂商**：换供应商只改这几个值，不动代码。
    #    当前默认走 DeepSeek；也可换智谱（glm-4-flash 免费档）等。
    # ⚠️ `base_url` 要**带上 `/v1`**：客户端会拼 `{base}/chat/completions`。
    #    DeepSeek 两种写法都认，但写全了换到只认 `/v1` 的服务时不会踩坑。
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    # 流式回答里要不要顺带要一份 `usage`（token 计量靠它）。
    # ⚠️ 实测 `deepseek-flash` **支持**（2026-10-17：最后一帧带
    #    `usage.prompt_tokens/completion_tokens/total_tokens`），所以默认开。
    # ⚠️ 关掉的后果不是"少一个指标"，而是**配额那本账算不出来**（等于没有熔断）——
    #    真遇到不兼容的端点时宁可选它，也别选"全站 400"。
    llm_stream_usage: bool = True

    # embedding
    # ⚠️ embedding_model / embedding_dim 是**元数据锁**：
    #    换模型必须全量重嵌入，否则检索质量会静默变差（AGENTS §7.1 约定 3）。
    # ⚠️ DeepSeek **不提供 embedding**，这一项必须另选一家（默认示例：阿里云百炼）。
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    # ⚠️ **默认值必须与已定选型一致**（`plan\PROJECT-PLAN.md` §4.2④）。
    #    理由不是"好看"，而是：`.env` 漏写这一行时**不会报错** ——
    #    如果默认值是个已废弃的模型名，就会静默用到错模型；检索质量变差，badcase 还归因不出来。
    #    （2026-10-15 之前这里还写着 `text-embedding-v3`，就是一颗这种雷。）
    embedding_model: str = "qwen3.7-text-embedding-flash"
    embedding_dim: int = 1024

    # rerank（精排）
    # 可以和 embedding 共用同一家 / 同一把 key，但必须用**专门的 rerank 模型**：
    # embedding 是双塔（query / doc 分开编码），rerank 是交叉编码，两者不能互相替代。
    rerank_enabled: bool = True
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_api_key: str = ""
    # ⚠️ 同 `embedding_model`：默认值必须与已定选型一致（§4.2④）。
    #    `gte-rerank-v2` 是**保底**选项、**不是**默认 —— 它没被校准过
    #    （见 `CALIBRATED_RERANK_MODELS`），配套阈值与它**不通用**。
    rerank_model: str = "qwen3.7-text-rerank"

    # 检索 / 生成
    retrieve_top_k: int = 20
    #: 召回范围（v1.3 阶段 B）。**相关度 ≠ 有效性**（`AGENTS §7.1` 约定 20）：
    #: · `current` = 只在**有资格被引用**的文档里召回（`content_status = 'active'`）← 产品态
    #: · `all`     = 全库召回（**含存档/草案/兄弟品牌/内部材料**），用于**复现历史数字**
    #: ⚠️ 这两个值不是"新旧配置"，是**两支尺子**：`all` 衡量"版本鉴别能力"（分数允许低），
    #:    `current` 是产品实际表现，**两者之差 = 知识库治理值多少分**（`PLAN §8.6`）。
    retrieval_scope: Literal["all", "current"] = "current"
    rerank_top_n: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 100
    rrf_k: int = 60
    # 低于该置信度直接**拒答**，不要硬答。
    # ⚠️ 比的是 **rerank 分数**。rerank 不可用时的余弦阈值是**另一个**配置项
    #    （`answer_min_cosine`）—— 两种分数**分布不同，一套阈值必然在一种口径上是错的**。
    # ⚠️ **这个值是实测标出来的，不是拍的**（2026-10-15，`qwen3.7-text-rerank`，20 条样本）：
    #      相关 0.786~0.990 ／ 无关 0.009~0.466 → 取 0.60（两侧余量 0.13 / 0.19）。
    #      旧值 0.35 配 qwen **误答 4/9**；而它本来是照 `gte-rerank-v2` 定的，
    #      那个模型同批数据**最高才 0.17** → 「gte + 0.35」会**全拒答**。
    # 📌 **2026-10-17 用 B 库重标（35 篇 / 231 块）：仍然成立，且更干净** ——
    #      可答 25 题 rerank 0.790~0.999（中位 0.936）／ 不可答 13 题 0.238~0.592（中位 0.435）
    #      ⟹ 间隙 0.592~0.790，0.60 落在间隙里：**正确拒答 13/13、误拒可答 0/25**。
    # 📌 **换 rerank 模型、或换语料之后都必须重新标定** —— 分布跟着这两者走。
    #    启动自检 `warn_if_rerank_calibration_missing()` 会检查"模型与阈值是否配套"。
    answer_min_score: float = 0.60
    # **rerank 不可用时**（`RERANK_ENABLED=false` 或调用失败）用来比**向量余弦**的阈值。
    # ⚠️ 为什么必须单列一个：余弦和 rerank 分**不是同一个量纲**。实测（2026-10-17，B 库）：
    #      可答 25 题余弦 0.409~0.688（中位 0.568）／ 不可答 13 题 0.352~0.578（中位 0.440）
    #      ⟹ **重叠区 0.409~0.578，几乎分不开**。拿 0.60 去比余弦会**误拒 19/25 个可答题**。
    # ⚠️ 0.45 这个值是在"两害相权"下选的：正确拒答 8/13、误拒可答 1/25。
    #    **故意偏向"宁可放过"** —— 误拒（该答却拒）比漏拒更伤体验，而且漏拒还有引用可核对。
    # 📌 换句话说：**rerank 关掉时，拒答能力是显著下降的**。这是降级，不是等价配置。
    answer_min_cosine: float = 0.45
    # pg_trgm 的 `word_similarity` 下限。低于它的 chunk 不进融合。
    # ⚠️ 刻意设得很低：这一步**只做粗筛**，真正的取舍交给 RRF + rerank。
    #    把粗筛阈值调高 = 在检索早期就把可能正确的答案丢掉，后面再也救不回来。
    fulltext_min_similarity: float = 0.05
    # 多轮对话带几轮历史（按**消息条数**）。带太多会把模型带偏，且 token 成本线性上涨。
    # ⚠️ 2026-10-24 走查 P1-⑧ 从 6 抬到 **12**（≈ 6 轮问答）：只带 3 轮时，用户问到
    #    第四句就"忘了"前面说过什么，表现为"刚说过订单号，它又问我要一遍"。
    #    代价是每问一次的输入 token 大约翻倍 —— 已重跑生成层评测确认要点命中不掉分
    #    （`uv run python evals/run.py --layer generation --mode all`）。
    history_messages: int = 12

    # agent（阶段 7，`PLAN §13`）
    #: **agent 模式总闸**（最高级开关）。`true` = 允许用户选择 agent；`false` = 全站强制走 pipeline。
    #:
    #: ⚠️ **代码默认 `False`，但部署两边的 `.env` 都置 `true`**（产品默认是开）。
    #:    为什么代码默认不写 `True`：测试跑的就是仓库默认值，默认开会把
    #:    **500+ 个 pipeline 用例全部改道**；而新拉代码的人不填 env 就默认跑 agent，
    #:    也不是他想要的。“产品默认开”由 **env** 表达，代码默认保守。
    #: ⚠️ 它只是**总闸**：实际走哪条 = `open_agent_mode AND 用户选了 agent`。
    #:    用户偏好默认「快速」（见 `chat.py` 的 `_resolve_mode`）。
    open_agent_mode: bool = False
    #: 最多几步。**一次模型返回算一步**（带多个 `tool_calls` 也只算一步 ——
    #: 并行调用是省步数的，不该反过来罚它）。
    agent_max_steps: int = 6
    #: 整轮问答的总预算（秒）。超了 → **立刻回落确定性 pipeline**。
    #: ⚠️ 这个数字就是"把最多花多少的权力留在代码里"那句话的实体（`architecture §9.48`）。
    #: 📌 20 → **30**（2026-10-27 走查：用户接受更慢一点，但不愿看到"走了 1 步就回落"）。
    agent_total_budget_seconds: float = 30.0
    #: 单步预算（秒）—— 等模型一次返回的上限。卡住的某一步不该吃掉整轮预算。
    #: 📌 8 → **10**，同上。
    agent_step_timeout_seconds: float = 10.0
    #: 检索落空 / 置信度不够时，允许"换个说法再查一次"几次（自愈）。
    agent_max_retries: int = 1
    #: **模型调用失败**（供应商抖动 / 连接断）时的重试次数。
    #: ⚠️ 与 `agent_max_retries` **是两件事**，别合成一个：那个管"检索没查到再查",
    #:    这个管"这次调用根本没拿到回复"。2026-10-27 走查的真因就是这个 ——
    #:    用户看到"走了 1 步就回落"，日志是 `对话 API 连接失败`，
    #:    **调预算救不了它**（详见 `architecture §9.53 ③`）。
    agent_model_retries: int = 1
    # 外部模型 API 超时（秒）。2C2G 上不能让一个卡住的请求把连接池占死。
    model_request_timeout_seconds: float = 60.0

    # 上传入库
    # 上传文件的**停放目录**（api 写、worker 读，同一个容器卷 `gq-uploads`）。
    # ⚠️ 相对路径按进程 cwd 解析：容器里是 `/app` → `/app/uploads`。
    # ⚠️ 这个目录**只是传送带**，不是存储：worker 处理完就删。
    #    真正要长期留着的是**切好的块**（在 `chunks` 表里）。
    upload_dir: str = "uploads"
    # 单个文件上限。刻意很小：
    #   · 知识库语料是**散文**，2 MB 已经是一本小册子；
    #   · 2C2G 的机器上，一次入库要把它切块 + 逐块调 embedding（每块一次网络往返）。
    upload_max_bytes: int = 2 * 1024 * 1024
    # 单文档最多切出多少块 —— 超过就**失败**，不做部分入库。
    # ⚠️ 治的是病态输入：一整段没有任何换行的 1 MB 文本能切出上千块，
    #    也就是上千次 embedding 请求，会把这个队列堵住半小时。
    # ⚠️ 为什么"失败"而不是"只取前 400 块"：只取一部分 = 文档后半截**永远检索不到**，
    #    而且没人知道。宁可整篇失败、让用户看见原因。
    max_chunks_per_document: int = 400

    # 提问带图（query 侧多模态）
    # 单张图上限 **8 MB**（2026-10-24 走查 P1-⑦ 从 3 MB 抬上来）。
    # ⚠️ 正常路径根本到不了这个数：前端会先 canvas 压到**长边 ≤1600 / JPEG 0.85**，
    #    一张 6 MB 的手机截图传上来通常只有几百 KB。8 MB 是给"前端压不动"留的口子
    #    （HEIC、超大拼接图、canvas 失败）—— 与其让用户自己去缩图，不如收下来。
    # ⚠️ 必须小于 nginx 的 `client_max_body_size`（`deploy/nginx/grounded-qa.conf` 现在 12m）——
    #    超了会被 nginx 挡下并返回一个 **HTML 错误页**，前端解析不了，
    #    症状是"本地传得上去、部署后传不上"。这是我们刻意留的余量。
    # ⚠️ **改这个数必须同步改 nginx 那一行**（它是打进 nginx 镜像的，改完要 push 才会生效）。
    image_max_bytes: int = 8 * 1024 * 1024
    #: 送给读图模型的那份副本：长边上限 / JPEG 质量 / 体积上限。
    #: ⚠️ 为什么要缩：原图最大 8 MB，base64 之后请求体更大 —— 慢，且视觉 token 按像素涨。
    #: ⚠️ 为什么**不无脑转 JPEG**：截图 / 单据上的**细笔画文字**在 JPEG 里会出现振铃，
    #:    "1/7""8/B" 容易糊。所以 PNG 一律保留 PNG（无损），只有照片类才转 JPEG。
    image_model_max_edge: int = 1600
    image_model_jpeg_quality: int = 88
    image_model_max_bytes: int = 1024 * 1024

    # 读图（图 → 结构化文字）
    # ⚠️ **复用对话那一套**：`deepseek-flash` 本身就是多模态模型、同一把 key、同一个端点。
    #    留独立旋钮是为了以后能换别的多模态模型 —— **换模型不需要重嵌入任何东西**
    #    （图从不进向量空间，见 `architecture.md` §9.35）。
    #: 空字符串 = 回退到 `llm_*` 的值（见 `app/rag/vision.py`）。
    image_analysis_model: str = ""
    image_analysis_base_url: str = ""
    image_analysis_api_key: str = ""
    #: 关掉它 = 有图也不读，直接按 D10 的降级路回话（"图片没读成功，我只按文字回答"）。
    image_analysis_enabled: bool = True

    # 评测报告（只读）
    # 评测结果 JSON 所在目录。**相对仓库根**解析（容器里就是 `/app/evals/results`）。
    # ⚠️ 这些文件是**报告数据**（`evals/results/*.json`），进镜像；
    #    题集（`evals/questions/`）与 runner（`evals/run.py`）**不进镜像** ——
    #    服务器不跑评测。三者边界见 `AGENTS.md §13`。
    evals_results_dir: str = "evals/results"

    # 多租户 / 限流 / 配额（**成本熔断**，逻辑见 `app/core/limits.py`）
    default_tenant_id: str = "default"
    # 每个用户每分钟能发几**次**问答。⚠️ 比的是次数，不是 token（token 是下面那条）。
    # ⚠️ 这个数必须**明显小于** nginx 那道按 IP 的限额（120 次/分钟）——
    #    否则用户会先撞上 nginx 的 HTML 错误页，而不是我们那条说得出"还有几秒"的 JSON。
    rate_limit_per_minute: int = 30
    # 每个用户每分钟能**上传**几次。比问答严得多：一次上传要跑一遍切分 ＋ N 次 embedding，
    # 而且它走**队列串行**处理 —— 一个人连传 20 个文件会把队列堵住，别人只能等。
    upload_rate_limit_per_minute: int = 5
    # 每个用户每天能烧多少 token（**模型 API 回报的 usage 累加**，不是估算）。
    # 200k ≈ 一天几百次问答，够个人正常用；按 `deepseek-flash` 的价，跑满也就几块钱。
    token_quota_per_day: int = 200_000
    # 展示用的**估算单价**（元 / 百万 token）。⚠️ **默认 0 = 不估算**：
    # 各家价格会变，写死一个数字比不给更糟 —— 页面会明确说"未配单价"。
    # 配了之后配额页会多显示一个"估算成本"，并**同时把单价印出来**（成本数字必须可解释）。
    llm_price_per_million_tokens: float = 0.0

    # 数据库连接池
    # ⚠️ 这两个值**不是"越大越快"**：池子是上限，真正的约束是"同时有多少个请求在等连接"。
    # v1.1 压测实测（本机 12 核、20 并发打 `/api/v1/conversations`，交替测量）：
    #   5/5 → 348 req/s ｜ 10/10 → 330 req/s ⟹ **池大小无可分辨影响**（都在测量波动内）。
    # 所以保持 5/5：2C2G 上空闲连接也要占 PG 的内存，而 PG 的 mem_limit 只有 288 MB。
    # 📌 两个旋钮做成配置是**为了能测**，不是为了改默认值：见 `architecture.md` §9.33。
    postgres_pool_size: int = 5
    postgres_max_overflow: int = 5
    #: 取用连接前先 `SELECT 1` 探活。**每次取连接多一个来回**，换来"死连接不变用户 500"。
    #: v1.1 压测：交替测量下开/关**无可分辨差异**（348 vs 330，都在波动内）⟹ **保持开启**。
    #: 🔁 早期那次"关掉后 +22%"是**冷启动污染**，教训写在 `architecture.md` §9.33。
    postgres_pre_ping: bool = True
    #: 连接最大年龄（秒）。到点主动换新连接，比等 PG / 中间设备断掉再报错好。
    postgres_pool_recycle: int = 1800

    # 鉴权（JWT）
    # ⚠️ 默认值**仅供本地开发**；生产必须由环境变量覆盖（下面的 `_guard_production_secrets` 会拦）。
    #    它是"能签发任意身份"的钥匙，泄露 = 全站沦陷。
    #
    # ⚠️ 长度不是随便定的：**HS256 的密钥至少 32 字节**（RFC 7518 §3.2）。
    #    短于 32 字节时 PyJWT 会抛 `InsecureKeyLengthWarning` ——
    #    这类警告最容易被忽略，然后带着弱密钥上线。所以默认值直接给足长度。
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    # bcrypt 轮数。12 是 2024 年前后的常见取值；调高更安全但登录更慢。
    # ⚠️ 登录接口跑在**线程池**里（见 app/api/auth.py），不会卡住事件循环。
    password_hash_rounds: int = 12

    # 管理端 bootstrap（首次建库用）
    # ⚠️ 这两个值只在 `python -m app.db.seed` 里用一次；生产必须改掉默认值。
    admin_email: str = "admin@example.com"
    admin_password: str = DEFAULT_ADMIN_PASSWORD

    # 演示登录（走查阶段 P0-③）
    # 打开后 `POST /auth/demo-login` 会签发一个**演示成员**的 token（前端登录页出现
    # 「一键体验」按钮）。**默认关闭**。
    # ⚠️ 为什么做在服务端而不是前端硬编码账号：前端硬编码等于把密码打进 JS bundle，
    #    任何人 view-source 就能拿到。这里连密码都不存在 —— 账号是 find-or-create 出来的。
    # ⚠️ 演示账号是 `member`（不能上传/不能看评测/运维），且照常受"每人每分钟次数 / 每日 token"限制。
    demo_login_enabled: bool = False

    # API
    api_prefix: str = "/api/v1"

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """**生产环境不许用开发默认密钥启动。**

        ⚠️ 这类校验必须"启动就炸"。等跑到某个请求才发现，意味着它**已经上线了** ——
            而公网上一个 `dev-only-...` 的 JWT 密钥等于任何人都能伪造管理员身份。

        ⚠️ 只在 `APP_ENV != "dev"` 时生效，所以本地开发零摩擦。
            代价要说清楚：如果部署时忘了把 `APP_ENV` 改成 `prod`，这道闸**不会**拦你 ——
            它防的是"知道要上生产但忘了换密钥"，不是"忘了设 APP_ENV"。
        """
        if self.app_env == "dev":
            return self

        problems: list[str] = []

        secret_bytes = len(self.jwt_secret.encode("utf-8"))
        if self.jwt_secret == DEV_JWT_SECRET:
            problems.append("JWT_SECRET 仍是开发默认值")
        elif not self.jwt_secret.strip():
            problems.append("JWT_SECRET 是空的")
        elif secret_bytes < 32:
            problems.append(
                f"JWT_SECRET 只有 {secret_bytes} 字节，HS256 要求至少 32 字节（RFC 7518 §3.2）"
            )

        if problems:
            raise ValueError("配置不安全，拒绝启动 —— " + "；".join(problems))

        # ⚠️ **`ADMIN_PASSWORD` 故意不在这道闸里**（2026-10-25 挪走）。
        #    它只在 `python -m app.db.seed` 建库时被读一次，**跑流量时一次都不读**。
        #    拿"整站起不来"去担保一个建库变量，代价已经实测过一次：
        #    `.env` 里它空着 → api crash-loop（pydantic ValidationError）→
        #    `depends_on: condition: service_healthy` 把 nginx 一起拖住 →
        #    **整站连接被拒**；而且**回滚也救不了**（回滚只换镜像，`.env` 不在版本控制里，
        #    新旧镜像读同一份坏配置）。
        #    ⟹ 闸门挪到真正用它的地方（`app/db/seed.py::_guard_admin_password`，建库时硬失败），
        #      这里只留一条**告警**：站点照常起，但要让人在日志里看得见。
        if not self.admin_password.strip():
            logger.warning(
                "ADMIN_PASSWORD 是空的 —— 不影响运行（运行时不用它），但**首次建库会失败**："
                "seed 拒绝用空密码创建管理员。要重建库，请先在 .env 里设一个真实密码。"
            )
        elif self.admin_password == DEFAULT_ADMIN_PASSWORD:
            logger.warning(
                "ADMIN_PASSWORD 仍是默认值 —— 不影响运行，但**首次建库会失败**（理由同上）。"
            )

        return self

    # 飞书渠道（v1.2 第①项）
    #: 关掉开关就**不起长连接**（容器照常在跑，只是不连）—— 没配凭据的环境不该因此起不来。
    feishu_enabled: bool = False
    #: 自建应用的凭据（飞书开放平台 → 应用 → 凭证与基础信息）。
    #: ⚠️ **不是**群机器人 webhook —— 那个只能发、收不到用户消息（告警用的是它，见上一段）。
    #: ⚠️ 是**凭据**：只从 `.env` 读，**不打印、不进日志、不进仓库**。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    #: 飞书消息统一记在哪个账号名下（用邮箱指人）。
    #: ⚠️ **v1.2 先接一个"渠道账号"**：`open_id → 用户` 的映射表要管理界面和权限设计，
    #:    那是另一件事。单账号够把链路跑通、也够演示；多用户映射写进 `CANDIDATES.md`。
    feishu_channel_email: str = ""
    #: 回复要不要带上引用来源（纯文本编号列表）。飞书里点不了 `[1]`，所以给的是**文字版**。
    #: ⚠️ **默认关**（2026-10-27 用户定）：飞书是**顾客侧**，顾客不需要知道
    #:    "我们知识库里有哪几篇"，那一串 `[1] 《退换货总则》…` 只会让人怀疑答案是不是抄的。
    #:    内部核对去 Web（那边默认折叠、还能点「看原文」）。
    #:    判据一句话：**顾客侧只给结论和"能不能办"，内部侧给全部证据**（`architecture §9.53 ①`）。
    feishu_show_sources: bool = False

    # 可观测
    metrics_enabled: bool = True
    #: 看板去问哪台 Prometheus。**容器里用服务名**（compose 网络内解析）；
    #: 本机直跑 uvicorn 时它不存在 ⟹ 看板返回 `available=false`，这是预期行为。
    prometheus_url: str = "http://prometheus:9090"
    #: Prometheus 的抓取周期（秒）。看板用它算"样本多旧算陈旧"（3 个周期）。
    #: ⚠️ 与 `deploy/prometheus/prometheus.yml` 的 `scrape_interval` 是**两份配置**，
    #:    改一处就要改另一处；写成配置而不是散在代码里，至少只有一处常量。
    prometheus_scrape_interval_s: int = 30
    #: 告警推送的群机器人地址（飞书 / 企微，按 URL 域名自动判格式）。
    #: ⚠️ **空值 = 关闭**（只打一行日志、不轮询）—— 没配就报错会把"能用"变成"起不来"。
    #: ⚠️ 它是**凭据**：只从 `.env` 读，**不打印、不进日志、不进仓库**。
    alert_webhook_url: str = ""
    #: 搬运工的轮询周期（秒）。比抓取周期长：告警状态本来以分钟计。
    alert_poll_interval_s: int = 60

    #: ⚠️ **回环主机名统一换成字面量 `127.0.0.1`。**
    #: Windows 上 `localhost` **先解析成 `::1`**，而 Docker 只把端口发布在 IPv4
    #: （`127.0.0.1:55432`）上 —— asyncpg 去连 `::1` 时 SYN 没人应答，连接就停在 `SynSent`
    #: 好几轮重传（**看起来就是 `pytest` 跑着跑着不动了，一行报错都没有**）。
    #: 换成字面量还能顺手省掉一次 DNS。
    @classmethod
    def _dsn_host(cls, host: str) -> str:
        # ⚠️ 集合写成局部量而不是类属性：pydantic v2 会把以 `_` 开头的**无注解**类属性
        #    转成 `ModelPrivateAttr`，`in cls._X` 会直接 `TypeError`（这里踩过一次）。
        if host.strip().lower() in {"localhost", "::1", "[::1]"}:
            return "127.0.0.1"
        return host

    @property
    def postgres_effective_port(self) -> int:
        """实际该连哪个端口：**容器内 5432，宿主机 55432**。

        判断依据是 host 是不是回环地址 —— 很土，但准：
            · 宿主机（Windows 开发）：`POSTGRES_HOST=localhost` → 走映射端口
            · 容器内（compose）：`POSTGRES_HOST=postgres`（服务名）→ 走 5432
        """
        loopback = {"localhost", "127.0.0.1", "::1", ""}
        if self.postgres_host.strip().lower() in loopback:
            return self.postgres_host_port
        return self.postgres_port

    @property
    def database_url(self) -> str:
        """SQLAlchemy 异步连接串（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self._dsn_host(self.postgres_host)}:{self.postgres_effective_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串。"""
        return f"redis://{self._dsn_host(self.redis_host)}:{self.redis_port}/{self.redis_db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取全局配置（进程内单例）。

    用 lru_cache 而不是模块级常量：测试里改环境变量后可以 ``get_settings.cache_clear()`` 重读。
    """
    return Settings()


def warn_if_rerank_calibration_missing(settings: Settings | None = None) -> None:
    """启动自检：**拒答阈值必须与 rerank 模型配套** —— 不配套就打显眼的告警。

    ⚠️ 为什么需要它（这是本项目**第三次**遇到同一类病：**静默失效**）：

        `answer_min_score` 比的是 **rerank 分数**；rerank 不可用时退化为比**向量余弦**
        （`app/rag/pipeline.py` 的 `vector_cosine` 分支）。这两种分数**量纲与分布完全不同**，
        而且分布跟着 **模型 + 语料** 走 —— 同一批 20 条样本（2026-10-15）：

            qwen3.7-text-rerank  →  相关 0.786~0.990 ／ 无关 0.009~0.466
            gte-rerank-v2        →  最高才 0.17

        所以「换了模型却留着旧阈值」的结果是**全拒答或全放行**，而且**不报错**。

    ⚠️ **只告警，不阻止启动** —— 理由同 `app/db/checks.py`：
        阈值不配套时问答**仍然能跑**（只是判得不准）。为了一个"不准"而拒绝启动，
        等于把**质量问题**升级成**可用性事故**。

    Args:
        settings: 便于测试注入；不传则取全局单例。
    """
    settings = settings or get_settings()

    if not settings.rerank_enabled:
        logger.warning(
            "\n"
            + "=" * 72
            + "\n⚠️  RERANK_ENABLED=false —— 拒答阈值比的不再是 rerank 分数\n"
            + "=" * 72
            + f"\n当前 ANSWER_MIN_SCORE = {settings.answer_min_score}，"
            + "它是照 **rerank 分数** 校准的。\n"
            + "关掉 rerank 后，同一个阈值会被拿去比 **向量余弦相似度** —— 两者分布不同，\n"
            + "可能全拒答或全放行，而且**不会报错**。\n"
            + "修法：要么打开 rerank；要么针对向量余弦另标一个阈值（当前代码只有一套阈值）。\n"
            + "=" * 72
        )
        return

    if settings.rerank_model in CALIBRATED_RERANK_MODELS:
        return

    logger.warning(
        "\n"
        + "=" * 72
        + f"\n⚠️  rerank 模型 `{settings.rerank_model}` **没有校准记录**\n"
        + "=" * 72
        + f"\n当前 ANSWER_MIN_SCORE = {settings.answer_min_score}，"
        + "但这个阈值是给别的模型标的。\n"
        + "现象：**全拒答或全放行，且不报错**。\n"
        + "修法：拿一批「相关 ／ 无关」样本重新标定，再把模型名加进\n"
        + "      `app/core/config.py` 的 `CALIBRATED_RERANK_MODELS`（那是校准记录）。\n"
        + f"已校准过：{'、'.join(CALIBRATED_RERANK_MODELS) or '（无）'}\n"
        + "详见 `plan\\PROJECT-PLAN.md` §4.2⑤。\n"
        + "=" * 72
    )
