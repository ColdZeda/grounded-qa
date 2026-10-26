"""纠错反馈的接口测试（`app/api/feedback.py`）。

⚠️ 这里只验**边界与权限**，不验"界面长什么样"。四条重点：
1. 快照是**服务端取**的（前端伪造不了）；
2. 同一个人重复提交是**覆盖**，不是堆成多条；
3. 只能对**助手**消息提；
4. 越权一律 **404**（不是 403）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Message, MessageRole
from app.db.models.feedback import AnswerFeedback
from app.db.models.user import UserRole
from tests.test_chat_api import _new_conversation

FEEDBACK = "/api/v1/feedback"


async def _seed_answer(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, text: str
) -> Message:
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content=text,
        citations=[
            {
                "index": 1,
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "score": 0.91,
                "score_kind": "rerank",
                "snippet": "家具主体保修 2 年",
                "ordinal": 0,
            }
        ],
        refused=False,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


@pytest.mark.integration
async def test_feedback_stores_a_snapshot_on_the_server(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """提交反馈 → 快照由服务端取，工时端改不了。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="家具主体保修 2 年[1]。",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer", "note": "应该是 3 年"},
        headers=account.auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["verdict"] == "wrong_answer"
    assert body["answer_snapshot"] == "家具主体保修 2 年[1]。"
    assert body["citations_snapshot"][0]["score_kind"] == "rerank"


@pytest.mark.integration
async def test_second_feedback_overwrites_the_first(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """同一个人对同一条消息提两次 → **覆盖**（不然一个人点五次就是五条 badcase）。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="x",
    )
    payload = {"message_id": str(message.id), "verdict": "wrong_answer"}

    first = await client.post(FEEDBACK, json=payload, headers=account.auth(token))
    assert first.status_code == 201
    second = await client.post(
        FEEDBACK,
        json={**payload, "verdict": "wrong_citation", "note": "串号了"},
        headers=account.auth(token),
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"], "应该是同一条被改写"

    rows = (
        await session.scalars(select(AnswerFeedback).where(AnswerFeedback.message_id == message.id))
    ).all()
    assert len(rows) == 1


@pytest.mark.integration
async def test_only_assistant_messages_are_accepted(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """对**用户自己**的消息提反馈 → 422（那不是在纠错）。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    user_message = Message(
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        role=MessageRole.USER,
        content="保修多久",
        citations=[],
        refused=False,
    )
    session.add(user_message)
    await session.commit()

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(user_message.id), "verdict": "wrong_answer"},
        headers=account.auth(token),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_other_peoples_message_is_404(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """**别人会话里的消息 → 404 而不是 403**（403 等于承认它存在，可被枚举）。"""
    owner = await make_account(label="fb-a")
    other = await make_account(label="fb-b")
    owner_token = await login(owner.email, owner.password)
    other_token = await login(other.email, other.password)
    conversation = await _new_conversation(client, owner_token)
    message = await _seed_answer(
        session,
        tenant_id=owner.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="y",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer"},
        headers=other.auth(other_token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
async def test_other_requires_a_note(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """选「其他」必须写一句 —— 否则这条记录对未来毫无用处。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="z",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "other"},
        headers=account.auth(token),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_list_is_admin_only(client: Any, make_account, login) -> None:
    """清单只给管理员（那是治理清单，不是个人收藏夹）。"""
    admin = await make_account(label="fb-admin", role=UserRole.ADMIN)
    member = await make_account(label="fb-member", role=UserRole.MEMBER)
    admin_token = await login(admin.email, admin.password)
    member_token = await login(member.email, member.password)

    ok = await client.get(FEEDBACK, headers=admin.auth(admin_token))
    assert ok.status_code == 200
    denied = await client.get(FEEDBACK, headers=member.auth(member_token))
    assert denied.status_code == 403
