"""全局配置。

⚠️ 项目级约定（AGENTS.md §7.1 约定 11）：
    **所有可调项走配置 / 环境变量，不硬编码。**
    配额、阈值、模型名、chunk 大小都会变；写死了就要改代码。

⚠️ 模型全部走 API，不落本地模型（AGENTS 硬约束 §2）——
    所以这里只有 base_url / api_key / model 名，没有模型路径。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    postgres_port: int = 5432
    postgres_db: str = "grounded_qa"
    postgres_user: str = "grounded"
    postgres_password: str = "change-me"

    # ---------- Redis ----------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_maxmemory: str = "96mb"

    # ---------- 对话 / 多模态（智谱免费档）----------
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"

    # ---------- embedding / rerank（百炼）----------
    # ⚠️ embedding_model / embedding_dim 是**元数据锁**：
    #    换模型必须全量重嵌入，否则检索质量会静默变差（AGENTS §7.1 约定 3）。
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_api_key: str = ""
    rerank_model: str = "gte-rerank-v2"

    # ---------- 检索 / 生成 ----------
    retrieve_top_k: int = 20
    rerank_top_n: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 100
    rrf_k: int = 60
    answer_min_score: float = 0.35

    # ---------- 多租户 / 限流 / 配额 ----------
    default_tenant_id: str = "default"
    rate_limit_per_minute: int = 30
    token_quota_per_day: int = 200_000

    # ---------- 管理端 ----------
    admin_username: str = "admin"
    admin_password: str = "change-me"

    # ---------- 可观测 ----------
    metrics_enabled: bool = True

    @property
    def database_url(self) -> str:
        """SQLAlchemy 异步连接串（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
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
