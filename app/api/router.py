"""API 路由汇总。

⚠️ 所有业务路由都挂在这个 router 下，由 `app/main.py` 加统一前缀（`settings.api_prefix`）。
    前缀**不写死在这里** —— 改版本号（`/api/v1` → `/api/v2`）应该只改一处配置。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    admin,
    auth,
    chat,
    conversations,
    documents,
    images,
    monitoring,
    orders,
    reports,
    usage,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)
# 订单：**只读**、仅本人。它存在的理由只有一个 —— 让图里的单号有处可去。
api_router.include_router(orders.router)
# 提问带图：先上传拿 `image_id`，再带它去提问（不复用文档上传那条路：前者是附件，后者要入库）。
api_router.include_router(images.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)
# 配额页（读 Redis 里的记账）。**业务面**：每个登录用户都能看自己的。
api_router.include_router(usage.router)
# 运维看板（读 Prometheus）。放最后：它是**运维面**，不是业务 API。
api_router.include_router(monitoring.router)

__all__ = ["api_router"]
