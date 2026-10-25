"""文档 / 会话的**管理动作**（2026-10-25 走查：能传不能删，等于没有管理）。

守三件事：
· **删除是硬删**，连带从属数据（切片的 `ON DELETE CASCADE`、会话里的消息）一起没；
· **越权一律 404**（不是 403）—— 403 等于承认这个 id 存在，可被用来枚举；
· 列表里能看到的东西，**权限判定必须和删除接口是同一个**（`_visible_to` / 租户仓储）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models.conversation import Channel, Conversation, Message, MessageRole
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix


async def _seed_document(session, tenant_id: uuid.UUID, *, blocks: int = 3) -> Document:
    document = Document(
        tenant_id=tenant_id,
        filename="upload/退货政策.md",
        content_type="text/markdown",
        size_bytes=512,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
    )
    session.add(document)
    await session.flush()
    for ordinal in range(blocks):
        session.add(
            Chunk(
                tenant_id=tenant_id,
                document_id=document.id,
                ordinal=ordinal,
                content=f"第 {ordinal} 块",
            )
        )
    await session.commit()
    return document


async def _seed_conversation(session, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
    conversation = Conversation(
        tenant_id=tenant_id, user_id=user_id, title="（自测）会话", channel=Channel.WEB
    )
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content="问一句",
        )
    )
    await session.commit()
    return conversation


# ------------------------------------------------------------
# 文档
# ------------------------------------------------------------
async def test_document_list_shows_chunk_count(client, login, make_account, session) -> None:
    """列表要给出"切成几块" —— 切分质量是检索质量的第一道门，看不出来就没人会发现切坏了。"""
    account = await make_account(label="docs-count")
    await _seed_document(session, account.tenant_id, blocks=4)
    token = await login(account.email, account.password)

    body = (await client.get(f"{PREFIX}/documents", headers=account.auth(token))).json()

    assert body["items"][0]["chunk_count"] == 4


async def test_delete_document_removes_chunks(client, login, make_account, session) -> None:
    account = await make_account(label="docs-del")
    document = await _seed_document(session, account.tenant_id, blocks=2)
    token = await login(account.email, account.password)

    response = await client.delete(f"{PREFIX}/documents/{document.id}", headers=account.auth(token))

    assert response.status_code == 204
    remaining = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id)
    )
    # 切片靠数据库的 ON DELETE CASCADE 一起没（不是应用层逐个删）
    assert remaining == 0
    listing = (await client.get(f"{PREFIX}/documents", headers=account.auth(token))).json()
    assert listing["total"] == 0


async def test_delete_document_is_404_for_other_tenants(
    client, login, make_account, session
) -> None:
    owner = await make_account(label="docs-owner")
    document = await _seed_document(session, owner.tenant_id)
    outsider = await make_account(label="docs-outsider")
    token = await login(outsider.email, outsider.password)

    response = await client.delete(
        f"{PREFIX}/documents/{document.id}", headers=outsider.auth(token)
    )

    assert response.status_code == 404


# ------------------------------------------------------------
# 会话
# ------------------------------------------------------------
async def test_delete_conversation_removes_messages(client, login, make_account, session) -> None:
    account = await make_account(label="conv-del")
    conversation = await _seed_conversation(
        session, tenant_id=account.tenant_id, user_id=account.user_id
    )
    token = await login(account.email, account.password)

    response = await client.delete(
        f"{PREFIX}/conversations/{conversation.id}", headers=account.auth(token)
    )

    assert response.status_code == 204
    remaining = await session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    assert remaining == 0


async def test_member_cannot_delete_someone_elses_conversation(
    client, login, make_account, session
) -> None:
    """普通成员删别人的会话 → **404**（不是 403）：不能让人从状态码看出"这条存在"。"""
    admin = await make_account(role=UserRole.ADMIN, label="conv-admin")
    conversation = await _seed_conversation(
        session, tenant_id=admin.tenant_id, user_id=admin.user_id
    )
    tenant = await session.scalar(select(Tenant).where(Tenant.id == admin.tenant_id))
    assert tenant is not None
    member = await make_account(role=UserRole.MEMBER, label="conv-member", tenant=tenant)
    token = await login(member.email, member.password)

    response = await client.delete(
        f"{PREFIX}/conversations/{conversation.id}", headers=member.auth(token)
    )

    assert response.status_code == 404
