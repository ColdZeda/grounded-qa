"""改自己的密码（走查阶段 P0-②）。

断言重点：**旧密码必须验**（token 泄露时不能被换掉密码）、
新密码立刻生效且旧密码失效、超 bcrypt 72 字节要 400 而不是静默截断。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix
NEW_PASSWORD = "brand-new-password-123"


async def test_change_password_then_old_one_stops_working(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    account = await make_account()
    token = await login(account.email, account.password)

    resp = await client.post(
        f"{PREFIX}/auth/change-password",
        json={"old_password": account.password, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text

    # 新密码能登录（login fixture 失败会直接断言失败）
    assert await login(account.email, NEW_PASSWORD)

    # 旧密码失效 → 401
    old = await client.post(
        f"{PREFIX}/auth/login",
        data={"username": account.email, "password": account.password},
    )
    assert old.status_code == 401


async def test_wrong_old_password_is_400_not_401(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    """⚠️ 必须是 **400**：401 会被前端的"token 过期"钩子接走、把用户踢去登录页。"""
    account = await make_account()
    token = await login(account.email, account.password)

    resp = await client.post(
        f"{PREFIX}/auth/change-password",
        json={"old_password": "not-the-real-one", "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert "原密码" in resp.text


async def test_same_password_is_rejected(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    account = await make_account()
    token = await login(account.email, account.password)

    resp = await client.post(
        f"{PREFIX}/auth/change-password",
        json={"old_password": account.password, "new_password": account.password},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400


async def test_new_password_over_bcrypt_limit_is_400(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    account = await make_account()
    token = await login(account.email, account.password)

    resp = await client.post(
        f"{PREFIX}/auth/change-password",
        json={"old_password": account.password, "new_password": "a" * 80},
        headers={"Authorization": f"Bearer {token}"},
    )

    # 80 字符 > max_length=64 → 被 pydantic 拦在门口
    assert resp.status_code == 422


async def test_change_password_requires_login(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/auth/change-password",
        json={"old_password": "x", "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 401
