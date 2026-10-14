"""SQLAlchemy 声明基类 + 通用 Mixin。

⚠️ **多租户是本项目的地基**（计划书 §5）：**每一张业务表都必须带 `tenant_id`**。
    这件事不能靠"记得写" —— 靠 `TenantMixin` 强制：继承它 = 自动获得
    `tenant_id` 外键 + 索引。**表里没有 tenant_id，就是设计错误。**

⚠️ **命名约定（`NAMING_CONVENTION`）不是装饰品**：
    PostgreSQL 默认给约束起名（`users_email_key` 之类），但名字依赖具体版本与建表顺序。
    Alembic 的 autogenerate 靠**约束名**比对差异 —— 名字不稳定，迁移就会误判"有变化"，
    于是生成一堆空迁移，甚至写错 DROP。**一次定死，永不修改。**

⚠️ **为什么枚举用 `native_enum=False`（VARCHAR + CHECK）而不是 PG 原生 ENUM**：
    PG 原生 enum 加一个取值要 `ALTER TYPE ... ADD VALUE`，而这条语句在 PG 12 以前
    **不能在事务里跑**，Alembic 迁移默认又在事务里 —— 加个状态值就得写手工迁移。
    VARCHAR + CHECK 改起来只是 drop/add 一个约束，**代价小得多**。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum[EnumT: enum.Enum](enum_cls: type[EnumT], name: str) -> SAEnum:
    """把 Python 枚举映射成 **VARCHAR + CHECK**（不是 PG 原生 ENUM）。

    ⚠️ `values_callable` 是必须的：SQLAlchemy 默认存枚举的 **`.name`**（如 `ADMIN`），
        而不是 `.value`（如 `admin`）。不指定的话，DB 里躺的是大写名字，
        和 API 里对外暴露的小写值对不上 —— 排查起来很费劲。

    ⚠️ 这里 `create_constraint=False`（= SQLAlchemy 默认值），CHECK 约束**由
        `enum_check()` 显式生成**。原因：`create_constraint=True` 时约束会被同时挂在
        「列类型」和「表约束」两处，Alembic autogenerate 会**生成两条重复的 CHECK**
        （一条叫 `document_status`、一条叫 `ck_documents_document_status`）。
        显式写就没有这个歧义，也更容易看懂迁移里到底建了什么。
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [m.value for m in cls],
    )


def enum_check[EnumT: enum.Enum](column: str, enum_cls: type[EnumT], name: str) -> CheckConstraint:
    """为一个"枚举列"生成 CHECK 约束（配合 `pg_enum` 使用）。

    ⚠️ 为什么还要数据库层面的校验，明明 Python 侧已经是枚举了：
        数据不只从 ORM 进来 —— 运维脚本、`psql` 手工修数据、将来的数据迁移都会写这张表。
        没有 CHECK，一个手滑的 `UPDATE users SET role='admn'` 能悄悄写进去，
        然后在**几周后**表现为"某个人登录后什么按钮都没有"。**让数据库来兜底。**

    用法：`__table_args__ = (enum_check("role", UserRole, "user_role"),)`
    """
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class UUIDPrimaryKeyMixin:
    """主键：UUID v4，由**应用侧**生成（不是数据库序列）。

    为什么不用自增整数：
        自增 ID 会把"我们有多少租户 / 多少用户"泄露给外部（`/users/3` → 一眼看出体量），
        且将来分库 / 合并数据时必然冲突。UUID 没有这个问题。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """创建 / 更新时间。用 `server_default=func.now()`，时间由**数据库**说了算。

    为什么不交给 Python 的 `datetime.now()`：
        应用容器和数据库可能有时间偏差，多副本时更乱。统一由数据库打时间戳，
        **同一个库里的所有时间可比**。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """**多租户隔离的地基**：给表加上 `tenant_id` 外键 + 索引。

    ⚠️ `declared_attr` 是必需的 —— Mixin 里的 `ForeignKey` 必须等目标表注册后才能解析，
        写成普通类属性会在导入期就报 `NoReferencedTableError`。

    ⚠️ `ondelete="CASCADE"`：删租户时它的数据一并消失。这是**有意的** ——
        但应用层**不要**提供"删租户"接口（那是运营动作，只能手工做）。
    """

    @declared_attr
    @classmethod
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PGUUID(as_uuid=True),
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
