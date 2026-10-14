"""租户表。

**这是整个多租户体系的天花板**：其余每张业务表都通过 `tenant_id` 挂到它下面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个租户 = 一个隔离域（一家公司 / 一个团队）。

    ⚠️ `slug` 是**人和脚本用来指代租户**的稳定短名（`.env` 里的 `DEFAULT_TENANT_ID`、
        运维脚本、URL 里都用它），所以必须唯一且**不要改**。
        改 slug 等于改名，会让已有配置全部指空。
    """

    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # ⚠️ 停用租户用 `is_active=False`，**不要删行** —— 删了它的数据会级联消失。
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list[User]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"
