"""飞书会话**分桶**（走查 P1-⑥）：群聊里按 `open_id` 分，私聊一人一个。

⚠️ 这个文件测的是**最容易出人命的那条**：群里的人要是共享一个会话，
    A 的追问就会读到 B 的上下文 —— 把别人的单号、地址串进回答里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import Account
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.feishu import FeishuChannel, FeishuEvent
from app.channels.feishu_names import short_id
from app.db.models.conversation import Channel, Conversation
from app.db.models.user import User


class _FakeSender:
    async def send_markdown(self, chat_id: str, markdown: str) -> None:  # pragma: no cover
        raise AssertionError("这些用例只测会话分桶，不该发消息")


class _FakeNames:
    """记录被问了几次 —— 顺带验"7 天内不重复解析"。"""

    def __init__(self, *, person: str | None, group: str | None = None) -> None:
        self.person = person
        self.group = group
        self.user_calls = 0
        self.chat_calls = 0

    async def user_name(self, open_id: str) -> str | None:
        self.user_calls += 1
        return self.person

    async def chat_name(self, chat_id: str) -> str | None:
        self.chat_calls += 1
        return self.group


def _event(*, chat_id: str, open_id: str, is_group: bool) -> FeishuEvent:
    return FeishuEvent(
        chat_id=chat_id, open_id=open_id, message_id="om_x", text="保修多久", is_group=is_group
    )


def _channel(names: object | None = None) -> FeishuChannel:
    return FeishuChannel(sender=_FakeSender(), chat=object(), names=names)  # type: ignore[arg-type]


async def _user(session: AsyncSession, account: Account) -> User:
    user = await session.get(User, account.user_id)
    assert user is not None
    return user


async def test_group_buckets_per_open_id(session: AsyncSession, make_account: object) -> None:
    """**同一个群里两个人 = 两个会话。** 这是整个功能存在的理由。"""
    account: Account = await make_account(label="grp")  # type: ignore[operator]
    user = await _user(session, account)
    channel = _channel()

    first = await channel._conversation(
        session, user, _event(chat_id="oc_g", open_id="ou_aaa111", is_group=True)
    )
    second = await channel._conversation(
        session, user, _event(chat_id="oc_g", open_id="ou_bbb222", is_group=True)
    )
    again = await channel._conversation(
        session, user, _event(chat_id="oc_g", open_id="ou_aaa111", is_group=True)
    )

    assert first.id != second.id
    assert again.id == first.id  # 同一个人再问，还是同一个会话
    assert first.channel is Channel.FEISHU_GROUP
    assert first.external_chat_id == "oc_g"
    # 没给解析器 → 退成 open_id 后 6 位（两个会话因此不会长得一样）
    assert first.title == f"飞书用户 · {short_id('ou_aaa111')}"
    assert first.title != second.title


async def test_p2p_is_one_conversation_per_chat(
    session: AsyncSession, make_account: object
) -> None:
    account: Account = await make_account(label="p2p")  # type: ignore[operator]
    user = await _user(session, account)
    channel = _channel()

    first = await channel._conversation(
        session, user, _event(chat_id="oc_dm", open_id="ou_x1", is_group=False)
    )
    again = await channel._conversation(
        session, user, _event(chat_id="oc_dm", open_id="ou_x1", is_group=False)
    )

    assert first.channel is Channel.FEISHU_P2P
    assert again.id == first.id


async def test_real_names_are_cached_and_refreshed_after_a_week(
    session: AsyncSession, make_account: object
) -> None:
    """真名解析成功就用真名；**7 天内不再问**，过了 7 天才刷（群改名能跟上）。"""
    account: Account = await make_account(label="name")  # type: ignore[operator]
    user = await _user(session, account)
    names = _FakeNames(person="张三", group="售后服务群")
    channel = _channel(names)

    conversation = await channel._conversation(
        session, user, _event(chat_id="oc_n", open_id="ou_zzz", is_group=True)
    )

    assert conversation.title == "张三"
    assert conversation.external_title == "售后服务群"
    assert (names.user_calls, names.chat_calls) == (1, 1)

    # 立刻再问一句：不该再打飞书接口
    await channel._conversation(
        session, user, _event(chat_id="oc_n", open_id="ou_zzz", is_group=True)
    )
    assert (names.user_calls, names.chat_calls) == (1, 1)

    # 把"上次解析时间"拨回 8 天前 → 应该再解析一次
    conversation.external_title_at = datetime.now(UTC) - timedelta(days=8)
    await session.commit()
    names.person = "张三（改名）"
    await channel._conversation(
        session, user, _event(chat_id="oc_n", open_id="ou_zzz", is_group=True)
    )
    assert names.user_calls == 2
    assert conversation.title == "张三（改名）"


async def test_failed_resolution_still_records_the_attempt(
    session: AsyncSession, make_account: object
) -> None:
    """解析失败也要写 `external_title_at` —— 否则每条消息都会重试一次注定失败的 HTTP。"""
    account: Account = await make_account(label="noname")  # type: ignore[operator]
    user = await _user(session, account)
    names = _FakeNames(person=None, group=None)
    channel = _channel(names)

    conversation = await channel._conversation(
        session, user, _event(chat_id="oc_bad", open_id="ou_bad", is_group=True)
    )

    assert conversation.title == f"飞书用户 · {short_id('ou_bad')}"
    assert conversation.external_title == f"飞书群 · {short_id('oc_bad')}"
    assert conversation.external_title_at is not None

    await channel._conversation(
        session, user, _event(chat_id="oc_bad", open_id="ou_bad", is_group=True)
    )
    assert names.user_calls == 1  # 第二次没有重复解析

    rows = (
        await session.scalars(select(Conversation).where(Conversation.id == conversation.id))
    ).all()
    assert len(rows) == 1
