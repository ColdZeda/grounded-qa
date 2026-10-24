"""管理端（**仅管理员**）。

⚠️ 这里的"管理员"是**租户内的管理员**，不是超级管理员 ——
    每个端点都只能看到自己租户的东西。跨租户的运营动作**没有 API**，
    这是有意的：多租户系统里最危险的 bug 就是"某个 admin 接口忘了限定租户"，
    不给它入口，就少一类漏洞。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.deps import AdminUser, SessionDep
from app.core.security import PasswordTooLongError, hash_password
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.repository import TenantScopedRepository
from app.schemas.tenant import TenantOut
from app.schemas.user import UserCreate, UserSummary

router = APIRouter(prefix="/admin", tags=["admin"])

#: 极简邮箱校验。⚠️ 刻意不引 `email-validator`（多一个依赖）——
#: 这里挡的是"明显不是邮箱"的输入（前端表单会先拦），**真正的唯一性判据是数据库约束**。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


@router.post(
    "/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="在本租户内新建成员（仅管理员）",
)
async def create_user(
    payload: UserCreate,
    session: SessionDep,
    current_admin: AdminUser,
) -> User:
    """管理员建成员 —— 这是本项目**唯一的"加人"入口**（没有开放注册）。

    ⚠️ 三条隔离/安全边界：
        ① `tenant_id` **不由请求体决定**，由 `TenantScopedRepository.build()` 注入
           （传 `tenant_id` 会直接抛错）⟹ 管理员只可能往**自己租户**里加人；
        ② `email` 是**全局唯一**（一个邮箱只属于一个租户）→ 撞了返回 **409**，
           不要降级成"再建一个"；
        ③ 只存 bcrypt 哈希；超 72 字节直接 **400**（bcrypt 会静默截断，不能让它发生）。

    ⚠️ bcrypt 是 CPU 密集的（cost=12 时几十~上百毫秒），**必须丢进线程池**，
        否则会卡住事件循环 —— 登录接口就是这么做的（`auth.py`）。
    """
    email = payload.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱格式不正确")

    # ⚠️ 用**裸 select** 查重、而不是仓储层：`email` 是全局唯一，
    #    用仓储层会被注入 `tenant_id` 条件 → 查不出"别的租户已占用同一个邮箱"，
    #    最后撞在数据库唯一约束上变成 500。这里只查"是否已被谁占用"，不返回任何用户数据。
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已被使用")

    try:
        hashed = await run_in_threadpool(hash_password, payload.password)
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    repo = TenantScopedRepository(session, tenant_id=current_admin.tenant_id, model=User)
    user = repo.build(
        email=email,
        hashed_password=hashed,
        role=payload.role,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user
