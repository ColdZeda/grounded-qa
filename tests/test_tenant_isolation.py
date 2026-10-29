"""租户隔离（**集成测试**）—— 本项目最重要的一组测试。

⚠️ **多租户系统最经典的翻车方式：某个查询忘了 `WHERE tenant_id = ?`。**
    这组测试就是为了让那种 bug **当场变红**，而不是上线后被客户发现。

⚠️ **设计要点：让"只看自己的"这条规则帮不上忙，只剩租户过滤能挡。**
    做法 —— 让攻击者是**另一个租户的管理员**：
        管理员本来就能看本租户**所有人**的数据，所以"只看自己的"拦不住他。
        如果租户条件被漏写，他就能读到别人的数据 → 测试立刻失败。
    反例（**很常见但没用的假测试**）：拿"普通成员 A 去读成员 B 的数据"来测 ——
        就算租户条件完全没写，`user_id` 过滤也会挡住，测试照样绿。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.conversation import Conversation
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.db.repository import MissingTenantColumnError, TenantScopedRepository

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix


async def _create_conversation(client: AsyncClient, token: str, title: str) -> dict:
    resp = await client.post(
        f"{PREFIX}/conversations",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# HTTP 层：跨租户读


async def test_admin_of_another_tenant_gets_404_not_someone_elses_data(
    client: AsyncClient, make_account, login
) -> None:
    """**核心用例。** 租户 B 的管理员拿着租户 A 的会话 id 去查 → 必须 404。

    ⚠️ 为什么用"另一个租户的管理员"当攻击者：见模块 docstring。
    ⚠️ 为什么断言 404 而不是 403：403 等于承认"这条数据存在"，
        攻击者可以据此**枚举**出有效 id。404 什么都不透露。
    """
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)

    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)

    alice_conversation = await _create_conversation(client, alice_token, "alice 的机密会话")

    # bob 自己是别的租户的管理员 —— 在他自己租户里他什么都能看
    resp = await client.get(
        f"{PREFIX}/conversations/{alice_conversation['id']}",
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert resp.status_code == 404
    assert "机密" not in resp.text


async def test_conversation_list_never_contains_other_tenants_rows(
    client: AsyncClient, make_account, login
) -> None:
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)

    alice_conversation = await _create_conversation(client, alice_token, "alice 的会话")

    resp = await client.get(
        f"{PREFIX}/conversations", headers={"Authorization": f"Bearer {bob_token}"}
    )

    assert resp.status_code == 200
    assert resp.json() == []  # bob 的租户里一条都没有
    assert alice_conversation["id"] not in resp.text


# HTTP 层：租户**内部**再按用户分一层


async def test_member_cannot_read_a_colleagues_conversation(
    client: AsyncClient, make_account, login, session: AsyncSession
) -> None:
    """同租户内的普通成员之间也要隔离（**第二层隔离**）。"""
    alice = await make_account(label="alice", role=UserRole.MEMBER)
    tenant = await session.get(Tenant, alice.tenant_id)
    assert tenant is not None
    carol = await make_account(label="carol", role=UserRole.MEMBER, tenant=tenant)

    alice_token = await login(alice.email, alice.password)
    carol_token = await login(carol.email, carol.password)

    alice_conversation = await _create_conversation(client, alice_token, "alice 的私聊")

    resp = await client.get(
        f"{PREFIX}/conversations/{alice_conversation['id']}",
        headers={"Authorization": f"Bearer {carol_token}"},
    )

    assert resp.status_code == 404


async def test_admin_sees_all_conversations_inside_own_tenant(
    client: AsyncClient, make_account, login, session: AsyncSession
) -> None:
    """管理员在本租户内看得到别人的会话 —— 与上一条形成对照。

    ⚠️ 这条同时保证了上一条不是"因为接口坏了才 404"：
        同样的接口、同样的数据，管理员能看到、普通成员看不到，说明是**规则**在起作用。
    """
    member = await make_account(label="member", role=UserRole.MEMBER)
    tenant = await session.get(Tenant, member.tenant_id)
    assert tenant is not None
    boss = await make_account(label="boss", role=UserRole.ADMIN, tenant=tenant)

    member_token = await login(member.email, member.password)
    boss_token = await login(boss.email, boss.password)

    created = await _create_conversation(client, member_token, "成员建的会话")

    resp = await client.get(
        f"{PREFIX}/conversations/{created['id']}",
        headers={"Authorization": f"Bearer {boss_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_client_cannot_dictate_ownership_on_create(
    client: AsyncClient, make_account, login
) -> None:
    """请求体里塞 `tenant_id` / `user_id` 一律**无效** —— 归属只由服务端推导。

    ⚠️ 如果接口接受客户端指定的归属，"越权写"就成了一个 POST 的距离。
    """
    alice = await make_account(label="alice", role=UserRole.MEMBER)
    bob = await make_account(label="bob", role=UserRole.MEMBER)
    alice_token = await login(alice.email, alice.password)

    resp = await client.post(
        f"{PREFIX}/conversations",
        json={
            "title": "试图写进 bob 名下",
            "tenant_id": str(bob.tenant_id),
            "user_id": str(bob.user_id),
        },
        headers={"Authorization": f"Bearer {alice_token}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == str(alice.user_id)  # 仍是 alice 自己的
    assert body["user_id"] != str(bob.user_id)


async def test_admin_user_list_is_tenant_scoped(client: AsyncClient, make_account, login) -> None:
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)

    resp = await client.get(
        f"{PREFIX}/admin/users", headers={"Authorization": f"Bearer {alice_token}"}
    )

    assert resp.status_code == 200
    emails = {row["email"] for row in resp.json()}
    assert alice.email in emails
    assert bob.email not in emails
    # 顺带确认没有把密码哈希带出去
    assert "hashed_password" not in resp.text


# 仓储层：直接测那道"焊死 tenant_id"的防线


async def test_repository_get_is_tenant_scoped(session: AsyncSession, make_account) -> None:
    alice = await make_account(label="alice")
    bob = await make_account(label="bob")

    conversation = Conversation(tenant_id=alice.tenant_id, user_id=alice.user_id, title="A 的")
    session.add(conversation)
    await session.commit()

    alice_repo = TenantScopedRepository(session, tenant_id=alice.tenant_id, model=Conversation)
    bob_repo = TenantScopedRepository(session, tenant_id=bob.tenant_id, model=Conversation)

    assert await alice_repo.get(conversation.id) is not None
    assert await bob_repo.get(conversation.id) is None  # ← 漏写 tenant 条件就会变成 not None


async def test_repository_count_is_tenant_scoped(session: AsyncSession, make_account) -> None:
    alice = await make_account(label="alice")
    bob = await make_account(label="bob")
    session.add_all(
        [
            Conversation(tenant_id=alice.tenant_id, user_id=alice.user_id, title="A1"),
            Conversation(tenant_id=alice.tenant_id, user_id=alice.user_id, title="A2"),
            Conversation(tenant_id=bob.tenant_id, user_id=bob.user_id, title="B1"),
        ]
    )
    await session.commit()

    alice_repo = TenantScopedRepository(session, tenant_id=alice.tenant_id, model=Conversation)
    bob_repo = TenantScopedRepository(session, tenant_id=bob.tenant_id, model=Conversation)

    assert await alice_repo.count() == 2
    assert await bob_repo.count() == 1


async def test_repository_build_refuses_client_supplied_tenant_id(
    session: AsyncSession, make_account
) -> None:
    """写入侧也不给"自己选租户"的机会。"""
    alice = await make_account(label="alice")
    bob = await make_account(label="bob")
    repo = TenantScopedRepository(session, tenant_id=alice.tenant_id, model=Conversation)

    with pytest.raises(ValueError, match="tenant_id"):
        repo.build(user_id=alice.user_id, title="x", tenant_id=bob.tenant_id)

    fresh = repo.build(user_id=alice.user_id, title="正常")
    assert fresh.tenant_id == alice.tenant_id


async def test_repository_refuses_model_without_tenant_column(
    session: AsyncSession,
) -> None:
    """忘了继承 `TenantMixin` 的模型**不许**进仓储层 —— 宁可炸，也别带着漏洞跑。

    ⚠️ `Tenant` 自己就是租户表，没有（也不该有）`tenant_id` 列，正好拿来做反例。
    """
    with pytest.raises(MissingTenantColumnError):
        TenantScopedRepository(session, tenant_id=uuid.uuid4(), model=Tenant)


async def test_every_business_table_actually_has_tenant_id() -> None:
    """**元测试**：所有业务表都必须有 `tenant_id` 列。

    ⚠️ 这条测试不测行为，测的是**结构约定**（计划书 §5 的地基要求）。
        将来有人加了一张新表却忘了继承 `TenantMixin`，这里会直接红。
        允许的例外只有 `tenants`（它自己就是租户表）。
    """
    from app.db import models  # noqa: F401  —— 触发模型注册
    from app.db.base import Base

    allowed_without_tenant = {"tenants"}
    missing = [
        table.name
        for table in Base.metadata.sorted_tables
        if table.name not in allowed_without_tenant and "tenant_id" not in table.columns
    ]

    assert missing == [], f"这些表没有 tenant_id，违反多租户地基约定：{missing}"
