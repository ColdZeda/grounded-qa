"""订单查询 —— **只读、租户内可查**（决策 D5，2026-10-24）。

⚠️ **口径变了**：从"只看得到自己的单"放宽到"**本租户内任意单号**"。
    Web 端定为**内部坐席工作台**（顾客侧走飞书），坐席手里只有顾客报的单号 ——
    让他只能查自己的单，这个功能就没有意义。→ `..\\plan\\PROJECT-PLAN.md` §12.1。

⚠️ **隔离线仍然是租户**：跨租户查不到，且**返回 404 而不是 403**
    （`AGENTS.md` §7.1 约定 14）—— 403 等于告诉对方"这个单号存在"，那就是枚举器。
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
    summary="按单号查订单（只读，租户内可查）",
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
        order_no=order_no,
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")
    return OrderOut.model_validate(order)
