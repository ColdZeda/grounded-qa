"""API 路由汇总。

⚠️ 所有业务路由都挂在这个 router 下，由 `app/main.py` 加统一前缀（`settings.api_prefix`）。
    前缀**不写死在这里** —— 改版本号（`/api/v1` → `/api/v2`）应该只改一处配置。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import admin, auth, chat, conversations, documents, reports

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
