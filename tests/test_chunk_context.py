"""引用卡片「看上下文」（`GET /documents/{id}/chunks?around=`，2026-10-25 P2.6）。

守四件事：
· **只返回相邻几块**，绝不返回整篇 —— "你看到的是片段"这件事要一直成立；
· 命中块被标出来（`hit`），**序号越界被夹到合法区间**（不报错、也不返回空列表）；
· **越权 / 不存在一律 404**（不给 403，否则可以用状态码差别枚举 document_id）；
· **普通成员也能用**（核对引用是每个提问者的能力），但文档列表仍然只给管理员。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix

BLOCKS = [
    "第 0 块：退换货总则的适用范围。",
    "第 1 块：无理由退换是签收后 7 天。",
    "第 2 块：质量问题退换是签收后 30 天。",
    "第 3 块：退款到账时间取决于支付渠道。",
    "第 4 块：定制商品不适用无理由退换。",
]


async def _seed(session, tenant_id: uuid.UUID) -> Document:
    """造一份 5 块的文档（**不走上传链路**：这里验的是"读"，不是"入库"）。"""
    document = Document(
        tenant_id=tenant_id,
        filename="senyu/10-退换货/10-退换货总则.md",
        content_type="text/markdown",
        size_bytes=1024,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
    )
    session.add(document)
    await session.flush()
    for ordinal, text in enumerate(BLOCKS):
        session.add(
            Chunk(tenant_id=tenant_id, document_id=document.id, ordinal=ordinal, content=text)
        )
    await session.commit()
    return document


async def test_returns_the_hit_block_and_its_neighbours(
    client, login, make_account, session
) -> None:
    account = await make_account(label="ctx")
    document = await _seed(session, account.tenant_id)
    token = await login(account.email, account.password)

    response = await client.get(
        f"{PREFIX}/documents/{document.id}/chunks",
        params={"around": 2, "window": 1},
        headers=account.auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5
    assert body["ordinal"] == 2
    assert [item["ordinal"] for item in body["items"]] == [1, 2, 3]
    assert [item["hit"] for item in body["items"]] == [False, True, False]
    # 内容原样返回（摘要被截断过，这里必须是**原文**）
    assert body["items"][1]["content"] == BLOCKS[2]


async def test_out_of_range_ordinal_is_clamped(client, login, make_account, session) -> None:
    """序号越界不报错：夹到最后一对块。**空列表比夹边更糟** —— 人以为文档是空的。"""
    account = await make_account(label="ctx-clamp")
    document = await _seed(session, account.tenant_id)
    token = await login(account.email, account.password)

    body = (
        await client.get(
            f"{PREFIX}/documents/{document.id}/chunks",
            params={"around": 99, "window": 1},
            headers=account.auth(token),
        )
    ).json()

    assert body["ordinal"] == 4
    assert [item["ordinal"] for item in body["items"]] == [3, 4]


async def test_window_zero_returns_only_the_hit_block(client, login, make_account, session) -> None:
    account = await make_account(label="ctx-w0")
    document = await _seed(session, account.tenant_id)
    token = await login(account.email, account.password)

    body = (
        await client.get(
            f"{PREFIX}/documents/{document.id}/chunks",
            params={"around": 0, "window": 0},
            headers=account.auth(token),
        )
    ).json()

    assert [item["ordinal"] for item in body["items"]] == [0]


async def test_other_tenants_document_is_404(client, login, make_account, session) -> None:
    """越权一律 404（不是 403）—— 403 等于承认"这个 id 存在"。"""
    owner = await make_account(label="ctx-owner")
    document = await _seed(session, owner.tenant_id)
    outsider = await make_account(label="ctx-outsider")
    token = await login(outsider.email, outsider.password)

    response = await client.get(
        f"{PREFIX}/documents/{document.id}/chunks", headers=outsider.auth(token)
    )

    assert response.status_code == 404


async def test_members_can_read_context_but_not_the_document_list(
    client, login, make_account, session
) -> None:
    """成员能核对引用，但**知识库列表仍然只给管理员**（两件事的权限故意不同）。"""
    admin = await make_account(role=UserRole.ADMIN, label="ctx-admin")
    document = await _seed(session, admin.tenant_id)
    # ⚠️ 成员必须和文档**在同一个租户**里，否则 404 的是"没权限"而不是"这段代码对不对"
    tenant = await session.scalar(select(Tenant).where(Tenant.id == admin.tenant_id))
    assert tenant is not None
    member = await make_account(role=UserRole.MEMBER, label="ctx-member", tenant=tenant)
    token = await login(member.email, member.password)

    context = await client.get(
        f"{PREFIX}/documents/{document.id}/chunks", headers=member.auth(token)
    )
    listing = await client.get(f"{PREFIX}/documents", headers=member.auth(token))

    assert context.status_code == 200
    assert listing.status_code == 403
