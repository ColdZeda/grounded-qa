"""会话：列表 / 详情 / 新建 / 删除。

⚠️ **这个模块是"租户隔离"的样板间。** 后续所有业务路由都照这个写法来：

    1. 一律用 `TenantScopedRepository` 查 —— 它自带 `tenant_id` 条件
    2. **绝不用** `session.get(Model, id)` —— 它只按主键查，是越权漏洞的经典写法
    3. 查不到就 **404**，不是 403 —— 403 等于告诉对方"这条数据存在，只是不给你看"，
       攻击者可以据此**枚举出有效 id**
    4. 租户内还要再按 `user_id` 分一层（管理员例外）：
       **隔离是两层的 —— 租户之间 + 租户内部按用户**
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, delete, func

from app.api.deps import CurrentUser, SessionDep
from app.db.models.conversation import Channel, Conversation
from app.db.models.user import User, UserRole
from app.db.repository import TenantScopedRepository
from app.schemas.conversation import (
    ConversationCreate,
    ConversationGroupOut,
    ConversationOut,
    ConversationSummaryOut,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _visible_to(stmt: Select[tuple[Conversation]], user: User) -> Select[tuple[Conversation]]:
    """在**已限定租户**的查询上，再叠加"这个用户能不能看到"的条件。

    ⚠️ 管理员能看到**本租户内**所有人的会话；普通成员只能看自己的。
        注意主语是"本租户内" —— 管理员**不是**跨租户的超级用户。
    """
    if user.role is UserRole.ADMIN:
        return stmt
    return stmt.where(Conversation.user_id == user.id)


@router.get("", response_model=list[ConversationOut], summary="会话列表（当前可见）")
async def list_conversations(
    session: SessionDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
    channel: Annotated[Channel | None, Query(description="只看这个渠道")] = None,
    chat_id: Annotated[str | None, Query(description="只看这个飞书群")] = None,
) -> list[Conversation]:
    """列出当前用户可见的会话，按最近更新倒序。

    ⚠️ `channel` / `chat_id` 是给**前端按组翻页**用的（2026-10-25）：一次拉全局 50 条
        会让一个组吃光配额，别的组一条都不出现。加了过滤之后，每组各自 `limit/offset`。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
    stmt = _visible_to(repo.select(), current_user)
    if channel is not None:
        stmt = stmt.where(Conversation.channel == channel)
    if chat_id is not None:
        stmt = stmt.where(Conversation.external_chat_id == chat_id)
    stmt = stmt.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


@router.get(
    "/summary",
    response_model=ConversationSummaryOut,
    summary="会话分组摘要（每组多少条）",
)
async def conversation_summary(
    session: SessionDep,
    current_user: CurrentUser,
) -> ConversationSummaryOut:
    """每组会话**一共有多少条** —— 前端靠它画组头「共 N 条 / 还有 N 条」。

    ⚠️ 为什么不让前端自己数：本页的数据是**按组翻页**的，手里只有"已加载的那几条"，
        数出来的永远是"这一页有多少"，说不出"一共多少"。
    ⚠️ 一次 `group by` 拿全，不做 N 次 count。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
    stmt = (
        _visible_to(repo.select(), current_user)
        .with_only_columns(
            Conversation.channel,
            Conversation.external_chat_id,
            Conversation.external_title,
            func.count().label("count"),
            func.max(Conversation.updated_at).label("latest_at"),
        )
        .group_by(Conversation.channel, Conversation.external_chat_id, Conversation.external_title)
    )
    rows = (await session.execute(stmt)).all()

    groups: list[ConversationGroupOut] = []
    total = 0
    for row in rows:
        channel = Channel(row.channel)
        total += int(row.count)
        if channel is Channel.FEISHU_GROUP:
            key = f"grp:{row.external_chat_id or 'unknown'}"
        elif channel is Channel.FEISHU_P2P:
            key = "p2p"
        else:
            key = "web"
        groups.append(
            ConversationGroupOut(
                key=key,
                channel=channel.value,
                external_chat_id=row.external_chat_id,
                external_title=row.external_title,
                count=int(row.count),
                latest_at=row.latest_at,
            )
        )

    # 固定顺序：内部 → 私聊 → 群（群之间按最近活动）
    order = {Channel.WEB.value: 0, Channel.FEISHU_P2P.value: 1, Channel.FEISHU_GROUP.value: 2}
    groups.sort(key=lambda item: (order[item.channel], -item.latest_at.timestamp()))
    return ConversationSummaryOut(total=total, groups=groups)


@router.get("/{conversation_id}", response_model=ConversationOut, summary="会话详情")
async def get_conversation(
    conversation_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Conversation:
    """按 id 取会话。**跨租户 / 越权一律 404。**"""
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
    stmt = _visible_to(repo.select().where(Conversation.id == conversation_id), current_user)

    conversation = await session.scalar(stmt)
    if conversation is None:
        # ⚠️ 刻意 404 而非 403：403 会泄露"这个 id 存在"，可被用来枚举
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return conversation


@router.post(
    "",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
async def create_conversation(
    payload: ConversationCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Conversation:
    """新建一个属于**当前用户 + 当前租户**的会话。

    ⚠️ 归属完全由服务端推导（`repo.build` 注入 `tenant_id`、这里显式传 `user_id`），
        请求体里没有这两个字段 —— 客户端**无法**指定把数据写到别人名下。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
    conversation = repo.build(user_id=current_user.id, title=payload.title)
    await repo.add(conversation)
    await session.commit()
    # 重新读一次，拿到数据库侧生成的 created_at / updated_at
    await session.refresh(conversation)
    return conversation


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（连同消息）",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """删掉一个会话**和它下面的全部消息**。

    ⚠️ **硬删、不可恢复** —— 内部工作台不做回收站（2026-10-25 定）。
    ⚠️ 权限和 `get_conversation` **完全一致**（自己的会话，或本租户管理员看得到的那些）：
        两处用同一个 `_visible_to`，否则会出现"看得见却删不掉 / 看不见却能删"。
    ⚠️ 越权一律 **404**（不给 403，否则可枚举 id）。
    ⚠️ 消息由数据库 `ON DELETE CASCADE` 一起删：`Conversation.messages` 是
        `lazy="raise"`，走 ORM 级联会抛"未加载"，所以直接发 Core DELETE。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
    stmt = _visible_to(repo.select().where(Conversation.id == conversation_id), current_user)
    conversation = await session.scalar(stmt)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    await session.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id, Conversation.tenant_id == current_user.tenant_id
        )
    )
    await session.commit()
