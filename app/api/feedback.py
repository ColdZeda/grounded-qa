"""答案纠错反馈的接口。

⚠️ **两种角色看到的不是同一件事**：
* 坐席（任何登录用户）：能对**自己有权访问的会话**里的助手消息提一条反馈；
* 管理员：能看**本租户全部**反馈（这是"治理清单"，不是个人收藏夹）。

⚠️ **不做点赞**：点踩只产生一个数字，对治理没有用；这里要的是"哪里不对 + 当时的答案"。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep, require_admin
from app.db.models.conversation import Conversation, Message, MessageRole
from app.db.models.feedback import AnswerFeedback, FeedbackVerdict
from app.db.repository import TenantScopedRepository
from app.schemas.feedback import FeedbackIn, FeedbackOut

router = APIRouter(prefix="/feedback", tags=["feedback"])


async def _load_message(session: SessionDep, user: CurrentUser, message_id: uuid.UUID) -> Message:
    """取出这条消息，并确认**当前用户有权访问它所在的会话**。

    ⚠️ 越权一律 404（`AGENTS §7.1` 约定 14）—— 403 等于承认"这条消息存在"。
    """
    message = await session.get(Message, message_id)
    if message is None or message.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is None or conversation.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    from app.db.models.user import UserRole

    if user.role is not UserRole.ADMIN and conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    return message


@router.post(
    "",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    summary="对一条助手回答提交纠错反馈",
)
async def create_feedback(
    payload: FeedbackIn,
    session: SessionDep,
    current_user: CurrentUser,
) -> AnswerFeedback:
    """提交纠错。**同一个人对同一条消息重复提交 = 覆盖**（不堆成多条 badcase）。

    ⚠️ 快照（答案原文 + 引用）在**这里**存，不是在前端传 ——
        前端传的话，改一行请求就能伪造一条"当时答案是 xxx"的记录。
    """
    message = await _load_message(session, current_user, payload.message_id)
    if message.role is not MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="只能对助手回答提反馈",
        )
    if payload.verdict is FeedbackVerdict.OTHER and not (payload.note or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="选「其他」时请写一句说明",
        )

    existing = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.message_id == message.id,
            AnswerFeedback.user_id == current_user.id,
        )
    )
    if existing is not None:
        # 覆盖：同一个人改了主意，不该留两条互相矛盾的说法
        existing.verdict = payload.verdict
        existing.note = (payload.note or "").strip() or None
        existing.answer_snapshot = message.content
        existing.citations_snapshot = list(message.citations or [])
        await session.commit()
        await session.refresh(existing)
        return existing

    feedback = AnswerFeedback(
        tenant_id=current_user.tenant_id,
        message_id=message.id,
        user_id=current_user.id,
        verdict=payload.verdict,
        note=(payload.note or "").strip() or None,
        answer_snapshot=message.content,
        citations_snapshot=list(message.citations or []),
    )
    session.add(feedback)
    await session.commit()
    await session.refresh(feedback)
    return feedback


@router.get(
    "",
    response_model=list[FeedbackOut],
    summary="本租户的纠错反馈清单（仅管理员）",
    dependencies=[Depends(require_admin)],
)
async def list_feedback(
    session: SessionDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AnswerFeedback]:
    """按时间倒序的反馈清单。

    ⚠️ **只用 `TenantScopedRepository`**（约定 12）：这张表里的每一行都带租户，
        忘了加 `WHERE tenant_id` 在这里等于把别家的问题清单给出去。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=AnswerFeedback)
    stmt = repo.select().order_by(AnswerFeedback.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())
