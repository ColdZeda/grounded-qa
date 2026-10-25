"""演示账号 = **只读管理员**（2026-10-25 定）。

守两条不变量：
    ① 它的 `role` 是 `admin` —— 知识库 / 评测报告 / 运维 / 全员会话都**看得见**（否则演示没价值）；
    ② 它的**写操作一律 403** —— 管理端写接口走 `AdminWriter`（`app/api/deps.py`）。

⚠️ 为什么第 ② 条是硬要求（不是为了洁癖）：
    · `DELETE /documents/{id}` 是**硬删**（无回收站）⟹ 访客能把整个知识库删空；
    · `POST /admin/users` 能**建成员** ⟹ 访客可以给自己开一个永久 admin，
      把"把演示开关关掉"这件事完全架空。
⚠️ 这条测试也要守"普通管理员没被误伤" —— 否则为了演示安全把正常运维手段砍掉了。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEMO_EMAIL, get_settings
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix


async def _ensure_tenant(session: AsyncSession) -> tuple[Tenant, bool]:
    """演示登录只在"默认租户"下面找 / 建账号，所以它得先存在。"""
    slug = get_settings().default_tenant_id
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is not None:
        return tenant, False
    tenant = Tenant(slug=slug, name="默认租户")
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant, True


async def _demo_token(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_login_enabled", True, raising=False)
    try:
        resp = await client.post(f"{PREFIX}/auth/demo-login")
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]
    finally:
        # ⚠️ 立刻关回去：本项目其它用例断言过"关闭时是 404"
        monkeypatch.setattr(settings, "demo_login_enabled", False, raising=False)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_demo_admin_can_read_admin_surfaces(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, created = await _ensure_tenant(session)
    try:
        token = await _demo_token(client, monkeypatch)
        for path in ("/documents", "/reports", "/admin/users", "/monitoring/snapshot"):
            resp = await client.get(f"{PREFIX}{path}", headers=_auth(token))
            # 运维看板在没有 Prometheus 的环境里会 5xx，那不算权限问题；只断"不是 401/403"
            assert resp.status_code not in (401, 403), f"{path} → {resp.status_code}"
    finally:
        await session.execute(delete(User).where(User.email == DEMO_EMAIL))
        if created:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()


async def test_demo_admin_cannot_write(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, created = await _ensure_tenant(session)
    try:
        token = await _demo_token(client, monkeypatch)
        headers = _auth(token)

        # 建成员 = 给自己留永久后门 → 必须 403
        created_user = await client.post(
            f"{PREFIX}/admin/users",
            json={
                "email": f"backdoor-{uuid.uuid4().hex[:8]}",
                "password": "whatever-123",
                "role": "admin",
            },
            headers=headers,
        )
        assert created_user.status_code == 403, created_user.text

        # 删文档（硬删、不可恢复）→ 403。⚠️ 用随机 id：权限依赖先于"文档存不存在"
        deleted = await client.delete(f"{PREFIX}/documents/{uuid.uuid4()}", headers=headers)
        assert deleted.status_code == 403, deleted.text

        # 上传文档 → 403
        uploaded = await client.post(
            f"{PREFIX}/documents",
            files={"file": ("x.md", b"# hello\n", "text/markdown")},
            headers=headers,
        )
        assert uploaded.status_code == 403, uploaded.text
    finally:
        await session.execute(delete(User).where(User.email == DEMO_EMAIL))
        if created:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()


async def test_real_admin_is_not_affected(client: AsyncClient, login, make_account) -> None:
    """普通管理员照旧能建成员 —— 别为了演示安全把正常运维手段砍掉。"""
    admin = await make_account(role=UserRole.ADMIN, label="writer-admin")
    token = await login(admin.email, admin.password)

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": f"new-{uuid.uuid4().hex[:8]}", "password": "whatever-123", "role": "member"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
