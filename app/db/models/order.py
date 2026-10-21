"""订单 —— **只读**的业务表，存在的理由只有一条：让图里的单号有处可去。

⚠️ **这不是"购物系统"的开头。** 我们不做下单 / 支付 / 购物车 / 库存 / 订单列表页
   （`architecture.md` §10 不做清单）。这张表只有一个消费者：
   「用户提问带图 → 图里有单号 → 精确查到这一单 → 把事实放进回答上下文」。

⚠️ **读它必须带两层过滤：`tenant_id` ＋ `user_id`。**
   知识库是**租户共有**的，订单是**用户私有**的 —— 这两件事的隔离级别不同。
   只按租户过滤的话，同租户的同事能查到你的单号；
   这也是本项目第一条"租户内按用户"的读取路径（见 `architecture.md` §9.35）。
   查不到 / 不是本人的 → **一律 404**（不要 403：403 等于承认这个单号存在）。

⚠️ 表里**只有事实，没有解释**。"签收 45 天该不该退"是知识库条款的事，
   不是订单表的事 —— 两者混在一起，模型就会把"已签收"当成"不能退"的结论。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_check,
    pg_enum,
)


class OrderStatus(enum.StrEnum):
    """订单状态。**故意只保留能影响客服判断的那几个**。"""

    PAID = "paid"  # 已付款、未发货
    SHIPPED = "shipped"  # 已发货、在途
    DELIVERED = "delivered"  # 已签收（`delivered_at` 有值）
    REFUNDING = "refunding"  # 退款中
    CANCELLED = "cancelled"  # 已取消


class Order(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """一条订单。**只读**：应用里没有任何写入路径，数据由 seed 造。"""

    __tablename__ = "orders"

    #: 归属用户。⚠️ 与 `tenant_id` 是**两道**过滤，缺一不可（见模块 docstring）。
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 对用户可见的单号（如 `SF10001`）。⚠️ **只在租户内唯一**，全局不唯一。
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), nullable=False
    )

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 签收时间。`NULL` = 还没签收 —— "签收多久了"这个问题**只能靠它算**，
    #: 不能靠"状态是 delivered 就假设是 30 天前"。
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: `[{"name": "实木床架", "qty": 1, "sku": "BED-2001"}]`
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    #: 金额用**分**存整数。浮点数存钱迟早出现 `0.1 + 0.2` 那种事故。
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_no"),
        enum_check("status", OrderStatus, "order_status"),
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_no} {self.status.value}>"
