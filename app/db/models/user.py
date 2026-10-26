"""用户表 + 角色。

⚠️ **一个刻意的简化：`email` 全局唯一，不是 (tenant_id, email) 复合唯一。**
    含义是「一个邮箱只属于一个租户」—— 跟多数 SaaS 一样（一个账号 → 一个工作区）。
    好处是登录只需邮箱，不用先选租户（少一个交互步骤、少一个越权面）。
    代价是"同一个人属于两家公司"要开两个邮箱。
    → 若将来要支持，改动集中在：去掉 `email` 的全局唯一约束、登录接口加 `tenant_slug`。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import DEMO_EMAIL
from app.db.base import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_check,
    pg_enum,
)

if TYPE_CHECKING:
    from app.db.models.tenant import Tenant


class UserRole(enum.StrEnum):
    """角色。

    ⚠️ v1.0 只要两种，**刻意不做完整 RBAC**（理由见 `docs\architecture.md` §10.2）：
        - `ADMIN`：能进管理端（上传知识库、看评测报告）
        - `MEMBER`：只能问答
    权限点写死在代码里（`app/api/deps.py` 的 `require_admin`），不建权限表。
    """

    ADMIN = "admin"
    MEMBER = "member"


class User(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """属于某个租户的用户。

    `tenant_id` 由 `TenantMixin` 提供（外键 + 索引），**不要在这里重复声明** ——
    重复声明会与 Mixin 拷进来的列冲突。
    """

    __tablename__ = "users"

    # 全局唯一 —— 登录只凭邮箱即可定位用户（见模块 docstring 的取舍说明）
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # ⚠️ 只存哈希，**永远不存明文**。字段名刻意叫 hashed_password（不是 password），
    #    这样任何 `user.password` 的写法都会直接 AttributeError。
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), default=UserRole.MEMBER, nullable=False
    )
    #: 用户个人偏好（JSONB）。当前只用一个键：
    #: `{"chat_mode": "fast" | "agent"}` —— 下次提问默认用哪条路。
    #:
    #: ⚠️ 为什么用 JSONB 而不是开一列：这类“界面小偏好”会一直长（主题/密度/默认渠道…），
    #:    每长一个就要一次迁移。但**只放界面偏好**，权限与计费相关的东西绝不进这里。
    preferences: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ⚠️ `lazy="raise"`：async 下"顺手访问关系属性"会触发隐式 IO，
    #    在 async 上下文里直接抛 MissingGreenlet，报错还很难懂。
    #    设成 raise 让它在**写代码时**就炸，逼你用 selectinload 显式加载。
    tenant: Mapped[Tenant] = relationship(back_populates="users", lazy="raise")

    # 数据库层面的角色校验（为什么需要它见 base.enum_check 的说明）
    __table_args__ = (enum_check("role", UserRole, "user_role"),)

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role.value}>"

    @property
    def is_demo(self) -> bool:
        """是不是一键演示账号（**只读管理员**）。

        ⚠️ 演示账号的 `role` 也是 `admin`（否则知识库 / 评测 / 运维全看不到，演示没意义），
            但它在管理端的**写接口**上被 `app/api/deps.py::require_admin_writer` 一律拦成 403：
            访客能看全部页面，却改不了任何数据（删文档不可恢复、建成员等于给自己留永久后门）。
        """
        return self.email == DEMO_EMAIL
