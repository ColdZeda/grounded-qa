"""订单相关的出参。

⚠️ **只读接口**，所以这里没有任何入参模型 —— 单号在路径里。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.order import OrderStatus


class OrderItemOut(BaseModel):
    """订单里的一件商品。字段都可缺省，别让脏数据把整个接口打挂。"""

    name: str = ""
    qty: int = 1
    sku: str | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_no: str
    status: OrderStatus
    placed_at: datetime
    #: 签收时间。`None` = 还没签收。
    #: ⚠️ 界面不要说"已签收 30 天"这种话 —— 天数由前端按它算，
    #:    服务器不替用户做"多久了"的解释（那是知识库条款该干的事）。
    delivered_at: datetime | None
    items: list[OrderItemOut] = []
    #: 金额用**分**。⚠️ 对外也保持分，不给浮点 —— 浮点在前端一算就有 0.01 的误差。
    amount_cents: int
    currency: str
