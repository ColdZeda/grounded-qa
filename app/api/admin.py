"""管理端（**仅管理员**）。

⚠️ 这里的"管理员"是**租户内的管理员**，不是超级管理员 ——
    每个端点都只能看到自己租户的东西。跨租户的运营动作**没有 API**，
    这是有意的：多租户系统里最危险的 bug 就是"某个 admin 接口忘了限定租户"，
    不给它入口，就少一类漏洞。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import AdminUser, SessionDep
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.repository import TenantScopedRepository
from app.schemas.tenant import TenantOut
from app.schemas.user import UserSummary

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/tenant", response_model=TenantOut, summary="当前租户信息（仅管理员）")
async def read_my_tenant(session: SessionDep, current_admin: AdminUser) -> Tenant:
    """返回当前管理员所属的租户。

    ⚠️ 这个查询**刻意没用** `TenantScopedRepository`：
        仓储层是靠 `WHERE tenant_id = ?` 隔离的，而 `tenants` 表**自己就是租户表**，
        没有（也不该有）`tenant_id` 列 —— 传进去会直接抛 `MissingTenantColumnError`。
        这里用 `id == current_admin.tenant_id` 达到同样的隔离效果，
        而 `tenant_id` 来自**服务端解析的 token**，客户端改不了。
    """
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == current_admin.tenant_id, Tenant.is_active.is_(True))
    )
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在或已停用")
    return tenant


@router.get("/users", response_model=list[UserSummary], summary="本租户用户列表（仅管理员）")
async def list_users(session: SessionDep, current_admin: AdminUser) -> list[User]:
    """列出**本租户**的用户。

    ⚠️ 隔离靠 `TenantScopedRepository`：它给查询注入了 `WHERE tenant_id = ?`，
        所以本租户管理员**看不到**别的租户的用户。
    """
    repo = TenantScopedRepository(session, tenant_id=current_admin.tenant_id, model=User)
    return list(await repo.list(limit=200))
