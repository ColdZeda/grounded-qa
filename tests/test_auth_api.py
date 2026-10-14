"""登录 / 当前用户 / 管理员鉴权（**集成测试**，需要真实数据库）。

覆盖的是"能被攻击者直接碰到"的那一层，所以断言的重点是**拒绝路径**：
    401 怎么给、403 什么时候给、响应里有没有漏字段。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.user import User, UserRole

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix


async def test_login_success_returns_bearer_token(client: AsyncClient, make_account, login) -> None:
    account = await make_account()

    token = await login(account.email, account.password)

    assert token
    # JWT 是三段式（header.payload.signature）
    assert token.count(".") == 2


async def test_login_response_never_leaks_password_hash(client: AsyncClient, make_account) -> None:
    """响应体里**不许**出现任何密码痕迹。"""
    account = await make_account()

    resp = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": account.email, "password": account.password},
    )

    assert resp.status_code == 200
    assert "hashed_password" not in resp.text
    assert account.password not in resp.text
    # 只暴露 token 本身
    assert set(resp.json()) == {"access_token", "token_type", "expires_in"}


async def test_login_with_wrong_password_is_401(client: AsyncClient, make_account) -> None:
    account = await make_account()

    resp = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": account.email, "password": "definitely-wrong"},
    )

    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


async def test_unknown_email_and_wrong_password_look_identical(
    client: AsyncClient, make_account
) -> None:
    """**不泄露邮箱是否注册过** —— 两种失败的响应必须一模一样。

    ⚠️ 如果"用户不存在"返回的文案或状态不同，攻击者就能先枚举出有效邮箱，
        再针对这些邮箱集中爆破。
    """
    account = await make_account()
    wrong_password = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": account.email, "password": "definitely-wrong"},
    )
    unknown_email = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": "nobody-here@example.com", "password": "definitely-wrong"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_me_requires_token(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/auth/me")

    assert resp.status_code == 401


async def test_me_rejects_malformed_token(client: AsyncClient) -> None:
    resp = await client.get(
        f"{PREFIX}/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert resp.status_code == 401


async def test_me_returns_identity_without_password_hash(
    client: AsyncClient, make_account, login
) -> None:
    account = await make_account(role=UserRole.ADMIN)

    token = await login(account.email, account.password)
    resp = await client.get(f"{PREFIX}/auth/me", headers=account.auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == account.email
    assert body["role"] == "admin"
    assert body["tenant_id"] == str(account.tenant_id)
    assert "hashed_password" not in resp.text


async def test_deactivated_user_cannot_login(
    client: AsyncClient, make_account, session: AsyncSession
) -> None:
    """停用后旧密码立刻失效 —— 这是运营侧的"一键踢人"。"""
    account = await make_account()
    user = await session.scalar(select(User).where(User.id == account.user_id))
    assert user is not None
    user.is_active = False
    await session.commit()

    resp = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": account.email, "password": account.password},
    )

    assert resp.status_code == 401


async def test_admin_endpoint_allows_admin(client: AsyncClient, make_account, login) -> None:
    account = await make_account(role=UserRole.ADMIN)

    token = await login(account.email, account.password)
    resp = await client.get(f"{PREFIX}/admin/tenant", headers=account.auth(token))

    assert resp.status_code == 200
    assert resp.json()["slug"] == account.tenant_slug


async def test_admin_endpoint_forbids_member(client: AsyncClient, make_account, login) -> None:
    """普通成员调管理端 → **403**（已登录，但权限不够）。"""
    account = await make_account(role=UserRole.MEMBER)

    token = await login(account.email, account.password)
    resp = await client.get(f"{PREFIX}/admin/tenant", headers=account.auth(token))

    assert resp.status_code == 403


async def test_admin_endpoint_requires_login(client: AsyncClient) -> None:
    """没带 token → 401（不是 403）—— 先认证，再鉴权。"""
    resp = await client.get(f"{PREFIX}/admin/tenant")

    assert resp.status_code == 401
