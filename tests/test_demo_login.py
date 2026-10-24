"""演示登录（走查阶段 P0-③）。

断言重点：
    ① 默认**关闭**时，接口对外**不存在**（404，而不是 403 —— 别让人从错误码猜出开关）；
    ② `capabilities` 如实反映开关（前端据此决定显示不显示按钮）；
    ③ 打开后签发的 token 能用，且身份是 **member**（演示入口不是后门）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import DEMO_EMAIL
from app.core.config import get_settings
from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix


async def test_capabilities_reports_demo_flag(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/auth/capabilities")
    assert resp.status_code == 200
    # 默认配置下是关的（测试环境读的是 .env，正常没开）
    assert resp.json()["demo_login"] is get_settings().demo_login_enabled


async def test_demo_login_is_404_when_disabled(client: AsyncClient) -> None:
    resp = await client.post(f"{PREFIX}/auth/demo-login")
    assert resp.status_code == 404


async def test_demo_login_issues_member_token_when_enabled(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    slug = settings.default_tenant_id

    # 造出默认租户（测试库里不一定有；演示登录只在它下面找/建账号）
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    created_tenant = tenant is None
    if tenant is None:
        tenant = Tenant(slug=slug, name="默认租户")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    monkeypatch.setattr(settings, "demo_login_enabled", True, raising=False)

    try:
        resp = await client.post(f"{PREFIX}/auth/demo-login")
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        assert token

        me = await client.get(f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        body = me.json()
        assert body["role"] == "member"  # 不是管理员
        assert body["email"] == DEMO_EMAIL

        # 幂等：再点一次还是同一个账号（不会每次建一个新用户）
        again = await client.post(f"{PREFIX}/auth/demo-login")
        assert again.status_code == 200
        me2 = await client.get(
            f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {again.json()['access_token']}"}
        )
        assert me2.json()["id"] == body["id"]
    finally:
        monkeypatch.setattr(settings, "demo_login_enabled", False, raising=False)
        await session.execute(delete(User).where(User.email == DEMO_EMAIL))
        if created_tenant:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()
