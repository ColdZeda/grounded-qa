"""订单查询 —— **只读、仅本人**。

⚠️ **为什么没有鉴权分级**：订单是用户私有数据，谁登录就看自己的那几单。
    没有"管理员看全租户订单"这种接口 —— 那属于运营面，本产品不做（`architecture.md` §10）。

⚠️ **查不到和不是本人的，返回同一个 404**（`AGENTS.md` §7.1 约定 14）：
    403 等于告诉对方"这个单号存在，只是不属于你" —— 那就是一个单号枚举器。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.orders import find_order
from app.schemas.order import OrderOut

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get(
    "/{order_no}",
    response_model=OrderOut,
    summary="按单号查订单（只读，仅本人）",
)
async def get_order(
    order_no: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> OrderOut:
    """按单号取一条订单。

    ⚠️ 单号**大小写不敏感**（`sf10001` 与 `SF10001` 等价）：
        用户从图里抄单号时不会注意大小写，这里没必要为难他。
        `find_order` 里做了 `.upper()` —— 与入库时的写法对齐。
    """
    order = await find_order(
        session,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        order_no=order_no,
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")
    return OrderOut.model_validate(order)
