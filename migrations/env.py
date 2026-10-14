"""Alembic 运行环境。

⚠️ **连接串只有一个来源**：`app.core.config`。
    不写进 `alembic.ini` —— 否则就会出现"应用连 A 库、迁移改 B 库"这种事，
    而它**不会报错**，只会让人对着一个"怎么改都没变化"的表发呆。

⚠️ **`import app.db.models` 不能删**：
    autogenerate 只扫描**已被导入**的模型类。漏导入 = 生成的迁移少一张表，
    **且不报错**（要等运行时报"表不存在"才发现）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# ⚠️ 这两个导入有副作用（注册模型到 Base.metadata），必须保留
from app.db import models  # noqa: F401
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # ⚠️ 必须显式给 encoding="utf-8"：`logging.config.fileConfig` 默认按**系统 locale** 打开文件。
    #    中文 Windows 的 locale 是 GBK —— 哪怕 alembic.ini 里只有一句中文注释，
    #    也会在**连数据库之前**就抛 UnicodeDecodeError（见 AGENTS.md §10）。
    #    顺带：正因为这个坑，alembic.ini 我们保持**纯 ASCII**。
    fileConfig(config.config_file_name, encoding="utf-8")

target_metadata = Base.metadata


def get_database_url() -> str:
    """从应用配置取异步连接串。"""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。

    用途：出交付文档 / 让 DBA 先审 SQL 再执行。
    """
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # ⚠️ 这两个开关让 autogenerate 能发现"字段类型改了"和"默认值改了"。
        #    不开的话，这类改动会被**静默忽略**，生成一个空迁移。
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用 asyncpg 驱动跑迁移。"""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
