"""管理员建成员（走查阶段 P0-①）。

这是本项目**唯一的"加人"入口** —— 没有开放注册。所以断言的重点和登录那组一样：
**拒绝路径**（谁能建、谁不能建、越界怎么给）比"建成功"更重要。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.user import User, UserRole

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix
NEW_PASSWORD = "member-password-123"


def _unique_email() -> str:
    return f"member-{uuid.uuid4().hex[:10]}@example.com"


async def _cleanup(session: AsyncSession, email: str) -> None:
    await session.execute(delete(User).where(User.email == email))
    await session.commit()


async def test_admin_can_create_member_who_then_logs_in(
    client: AsyncClient,
    make_account,
    login,
    session: AsyncSession,
) -> None:
    """管理员建出来的成员：① 立刻能登录；② 归在**同一个租户**；③ 响应里没有哈希。"""
    admin = await make_account(role=UserRole.ADMIN, label="adm")
    token = await login(admin.email, admin.password)
    email = _unique_email()

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": email, "password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["role"] == "member"  # 默认不是管理员
    assert body["is_active"] is True
    assert "hashed_password" not in body and "password" not in body

    # 同一个租户（靠"用这个账号登录成功"间接验证：登录只会命中本租户的用户）
    member_token = await login(email, NEW_PASSWORD)
    assert member_token

    me = await client.get(f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {member_token}"})
    assert me.status_code == 200
    assert me.json()["tenant_id"] == str(admin.tenant_id)

    await _cleanup(session, email)


async def test_duplicate_email_is_409_not_500(
    client: AsyncClient,
    make_account,
    login,
    session: AsyncSession,
) -> None:
    """`email` 是**全局唯一**：撞了要 409，不能让它落到数据库约束上变成 500。"""
    admin = await make_account(role=UserRole.ADMIN, label="adm")
    token = await login(admin.email, admin.password)
    email = _unique_email()
    payload = {"email": email, "password": NEW_PASSWORD}

    first = await client.post(
        f"{PREFIX}/admin/users", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"{PREFIX}/admin/users", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert second.status_code == 409

    await _cleanup(session, email)


async def test_member_cannot_create_user(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    """普通成员调管理端 → **403**（已登录但角色不够；越权看数据才是 404）。"""
    member = await make_account(role=UserRole.MEMBER, label="mem")
    token = await login(member.email, member.password)

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": _unique_email(), "password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403


async def test_password_over_bcrypt_limit_is_400(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    """bcrypt 上限 72 **字节**：超了要 400，绝不静默截断（截断 = 后半段不参与校验）。"""
    admin = await make_account(role=UserRole.ADMIN, label="adm")
    token = await login(admin.email, admin.password)

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": _unique_email(), "password": "a" * 80},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert "72" in resp.text


async def test_malformed_email_is_400(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    admin = await make_account(role=UserRole.ADMIN, label="adm")
    token = await login(admin.email, admin.password)

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": "not-an-email", "password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400


async def test_short_password_is_rejected_by_schema(
    client: AsyncClient,
    make_account,
    login,
) -> None:
    """<8 位由 pydantic 挡在门口（422），不进业务代码。"""
    admin = await make_account(role=UserRole.ADMIN, label="adm")
    token = await login(admin.email, admin.password)

    resp = await client.post(
        f"{PREFIX}/admin/users",
        json={"email": _unique_email(), "password": "1234567"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422
