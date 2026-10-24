"""登录 / 当前用户。

⚠️ **登录接口用 `OAuth2PasswordRequestForm`（form-urlencoded），不是 JSON。**
    这样 Swagger UI 右上角的 **Authorize 按钮直接可用** —— 面试演示时很重要，
    否则要手工拼 `Authorization` 头。
    （`app/schemas/auth.py` 里的 `LoginRequest` 留给前端走 JSON 时用。）
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core.security import (
    PasswordTooLongError,
    create_access_token,
    hash_password,
    verify_password,
)
from app.db.models.user import User
from app.schemas.auth import PasswordChange, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache(maxsize=1)
def _timing_decoy_hash() -> str:
    """一个"永远匹配不上"的哈希，只用来**抹平响应时间**。

    ⚠️ 为什么需要它：如果"用户不存在"时直接返回，而"密码错"时要跑一次 bcrypt（~100ms），
        那么**响应时间本身就泄露了"这个邮箱注册过没有"**。
        攻击者可以先枚举出有效邮箱，再针对性爆破。
        做法：用户不存在时也照样跑一次 bcrypt，只是拿这个假哈希去比。
    """
    return hash_password("timing-decoy-value-never-matches")


@router.post("/login", response_model=TokenOut, summary="登录（换取 access_token）")
async def login(
    session: SessionDep,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenOut:
    """用邮箱 + 密码换 JWT。

    ⚠️ **`form.username` 装的是邮箱** —— OAuth2 规范把这个字段叫 username，
        我们不另外改名（改名会让 Swagger 的 Authorize 弹窗对不上）。
    """
    email = form.username.strip().lower()

    user = await session.scalar(select(User).where(User.email == email))

    # ⚠️ bcrypt 是 CPU 密集的，**必须丢进线程池** —— 直接 await 会卡住整个事件循环，
    #    表现为"有人在登录，所有其他请求一起变慢"。
    hashed = user.hashed_password if user is not None else _timing_decoy_hash()
    password_ok = await run_in_threadpool(verify_password, form.password, hashed)

    if user is None or not password_ok or not user.is_active:
        # 四种失败（不存在 / 密码错 / 已停用 / 邮箱格式怪）**对外完全一样**
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role.value,
    )
    return TokenOut(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def read_me(current_user: CurrentUser) -> User:
    """返回当前用户。

    ⚠️ 这个端点看着"没什么用"，其实是**前端鉴权的探针**：
        页面加载时调一次，401 就去登录页，200 就拿到 `role` 决定显示不显示管理入口。
    """
    return current_user


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="改自己的密码（需验旧密码）",
)
async def change_password(
    payload: PasswordChange,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """改密码（走查阶段 P0-②）。

    ⚠️ **必须验旧密码**：token 泄露时，攻击者不该能直接把密码换掉把主人锁在门外。
    ⚠️ 旧密码错 → **400**，不是 401：401 会被前端的"token 过期"钩子接走、直接踢去登录页，
       而这里真正的问题是"你填的旧密码不对"，用户应该留在原地重填。
    ⚠️ 改完**不吊销已发出的 token**（JWT 无状态，要吊销得维护名单，属另一件事）——
       这是已知限制，写在这里免得被当成 bug。
    """
    verified = await run_in_threadpool(
        verify_password, payload.old_password, current_user.hashed_password
    )
    if not verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确")

    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与旧密码相同"
        )

    try:
        hashed = await run_in_threadpool(hash_password, payload.new_password)
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    current_user.hashed_password = hashed
    session.add(current_user)
    await session.commit()
