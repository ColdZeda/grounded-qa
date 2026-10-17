"""全局配置。

⚠️ 项目级约定（AGENTS.md §7.1 约定 11）：
    **所有可调项走配置 / 环境变量，不硬编码。**
    配额、阈值、模型名、chunk 大小都会变；写死了就要改代码。

⚠️ 模型全部走 API，不落本地模型（AGENTS 硬约束 §2）——
    所以这里只有 base_url / api_key / model 名，没有模型路径。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ⚠️ 两个"开发默认值"抽成常量，方便启动校验直接比对（也避免在多处重复字面量）。
# ⚠️ `DEV_JWT_SECRET` 给足 32 字节 —— 见 Settings.jwt_secret 的说明。
DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-in-production"
DEFAULT_ADMIN_PASSWORD = "change-me"

#: **已实测校准过** `answer_min_score` 的 rerank 模型。
#:
#: ⚠️ 判定只用「模型在不在这个表里」—— **在表外 = 从没校准过 = 那个阈值是抄来的**。
#:    表里的值是一句话记录，**仅供人看、不参与判定** ——
#:    一旦参与判定，就变成"改一处要同步两处"，那必然漂移（`AGENTS §4` 刻意不写条数就是这个道理）。
#:
#: 📌 **重新标定完，把新模型名加进来** —— 这是"校准记录"，不是配置项。
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

    # ---------- 应用 ----------
    app_version: str = "0.1.0"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # ---------- PostgreSQL ----------
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

    # ---------- Redis ----------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_maxmemory: str = "96mb"

    # ---------- 对话 / 多模态（任意 OpenAI 兼容端点）----------
    # ⚠️ 刻意**不绑定厂商**：换供应商只改这几个值，不动代码。
    #    当前默认走 DeepSeek；也可换智谱（glm-4-flash 免费档）等。
    # ⚠️ `base_url` 要**带上 `/v1`**：客户端会拼 `{base}/chat/completions`。
    #    DeepSeek 两种写法都认，但写全了换到只认 `/v1` 的服务时不会踩坑。
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"

    # ---------- embedding ----------
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

    # ---------- rerank（精排）----------
    # 可以和 embedding 共用同一家 / 同一把 key，但必须用**专门的 rerank 模型**：
    # embedding 是双塔（query / doc 分开编码），rerank 是交叉编码，两者不能互相替代。
    rerank_enabled: bool = True
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_api_key: str = ""
    # ⚠️ 同 `embedding_model`：默认值必须与已定选型一致（§4.2④）。
    #    `gte-rerank-v2` 是**保底**选项、**不是**默认 —— 它没被校准过
    #    （见 `CALIBRATED_RERANK_MODELS`），配套阈值与它**不通用**。
    rerank_model: str = "qwen3.7-text-rerank"

    # ---------- 检索 / 生成 ----------
    retrieve_top_k: int = 20
    rerank_top_n: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 100
    rrf_k: int = 60
    # 低于该置信度直接**拒答**，不要硬答。
    # ⚠️ 比的是 **rerank 分数**；rerank 不可用时退化去比**向量余弦**（见 `_should_refuse`）。
    # ⚠️ **这个值是实测标出来的，不是拍的**（2026-10-15，`qwen3.7-text-rerank`，20 条样本）：
    #      相关 0.786~0.990 ／ 无关 0.009~0.466 → 取 0.60（两侧余量 0.13 / 0.19）。
    #      旧值 0.35 配 qwen **误答 4/9**；而它本来是照 `gte-rerank-v2` 定的，
    #      那个模型同批数据**最高才 0.17** → 「gte + 0.35」会**全拒答**。
    # 📌 **换 rerank 模型、或换语料之后都必须重新标定** —— 分布跟着这两者走。
    #    启动自检 `warn_if_rerank_calibration_missing()` 会检查"模型与阈值是否配套"。
    answer_min_score: float = 0.60
    # pg_trgm 的 `word_similarity` 下限。低于它的 chunk 不进融合。
    # ⚠️ 刻意设得很低：这一步**只做粗筛**，真正的取舍交给 RRF + rerank。
    #    把粗筛阈值调高 = 在检索早期就把可能正确的答案丢掉，后面再也救不回来。
    fulltext_min_similarity: float = 0.05
    # 多轮对话带几轮历史（按消息条数）。带太多会把模型带偏，且 token 成本线性上涨。
    history_messages: int = 6
    # 外部模型 API 超时（秒）。2C2G 上不能让一个卡住的请求把连接池占死。
    model_request_timeout_seconds: float = 60.0

    # ---------- 多租户 / 限流 / 配额 ----------
    default_tenant_id: str = "default"
    rate_limit_per_minute: int = 30
    token_quota_per_day: int = 200_000

    # ---------- 鉴权（JWT）----------
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

    # ---------- 管理端 bootstrap（首次建库用）----------
    # ⚠️ 这两个值只在 `python -m app.db.seed` 里用一次；生产必须改掉默认值。
    admin_email: str = "admin@example.com"
    admin_password: str = DEFAULT_ADMIN_PASSWORD

    # ---------- API ----------
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

        # ⚠️ **空值必须先判，否则这道闸会漏掉它**（2026-10-17 补）：
        #    `.env` 里写成 `ADMIN_PASSWORD=`（占位没填）时，读出来是**空字符串**，
        #    它**不等于** `DEFAULT_ADMIN_PASSWORD`（`"change-me"`）——
        #    原来那句相等判断就**放行了**，然后 `seed` 会建一个**空密码的管理员**。
        #    这是"配置看起来填了、其实没填"的典型：**闸只比较了"是不是默认值"，
        #    没比较"是不是根本为空"**。
        if not self.admin_password.strip():
            problems.append("ADMIN_PASSWORD 是空的")
        elif self.admin_password == DEFAULT_ADMIN_PASSWORD:
            problems.append("ADMIN_PASSWORD 仍是默认值")

        if problems:
            raise ValueError("配置不安全，拒绝启动 —— " + "；".join(problems))

        return self

    # ---------- 可观测 ----------
    metrics_enabled: bool = True

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
            f"@{self.postgres_host}:{self.postgres_effective_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串。"""
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


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
