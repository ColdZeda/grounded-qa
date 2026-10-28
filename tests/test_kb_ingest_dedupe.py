"""同名文档「内容变了要换版」的判重口径（`app/kb/ingest.py::prepare_document`）。

2026-10-28 现场：语料补了一段「中国大陆 5 个城市」，本地 `--replace` 灌好了，
但 CD 里的 ingest **不带** `--replace` ⟹ 生产**静默跳过**、库里还是旧内容，而部署是绿的。
⟹ 判据改成「**同名同内容**才跳过；同名不同内容 = 新版本，自动换掉旧版」。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Document, DocumentStatus
from app.kb.ingest import prepare_document

NAME = "senyu/40-服务/41-安装服务与客服SLA.md"


@pytest.mark.integration
async def test_same_name_same_content_is_skipped(session: AsyncSession, make_account) -> None:
    """**内容没变** ⟹ 跳过（日常部署不该重复调 embedding）。"""
    account = await make_account(label="kb-dup")
    first = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"same body"
    )
    second = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"same body"
    )

    assert first.created is True
    assert second.created is False
    assert second.document.id == first.document.id
    assert "跳过" in second.note


@pytest.mark.integration
async def test_same_name_changed_content_is_replaced(session: AsyncSession, make_account) -> None:
    """**同名但内容变了** ⟹ 自动换版（旧行被删、新行是新 hash）。

    ⚠️ 以前这里返回"同名文档已入库过（要重来需显式替换）"⟹ 语料改了、部署绿、库里还是旧内容。
    """
    account = await make_account(label="kb-replace")
    old = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"v1 body"
    )
    await session.commit()
    old_id = old.document.id
    old.document.status = DocumentStatus.READY
    await session.commit()

    new = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"v2 body with new sentence"
    )
    await session.commit()

    assert new.created is True
    assert new.document.id != old_id
    rows = (
        await session.scalars(
            select(Document).where(
                Document.tenant_id == account.tenant_id, Document.filename == NAME
            )
        )
    ).all()
    assert [row.id for row in rows] == [new.document.id], "旧版必须被换掉，否则库里有两个版本"


@pytest.mark.integration
async def test_in_flight_same_name_is_not_replaced(session: AsyncSession, make_account) -> None:
    """同名文档**正在入库中**（PENDING/PROCESSING）⟹ 不换版（换一次等于白烧 embedding）。"""
    account = await make_account(label="kb-flight")
    first = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"v1 body"
    )
    await session.commit()

    again = await prepare_document(
        session, tenant_id=account.tenant_id, filename=NAME, data=b"v2 body"
    )

    assert again.created is False
    assert again.document.id == first.document.id
    assert "正在入库" in again.note
    assert uuid.UUID(str(first.document.id))  # 行还在
