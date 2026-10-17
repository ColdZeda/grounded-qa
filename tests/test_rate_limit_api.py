"""限流与配额的**接口层**行为（集成测试，需要真实数据库）。

⚠️ 这里验的不是"算法算得对不对"（那是 `test_limits.py`），而是**接入的位置对不对**：

    · 429 是不是**在落库之前**发的 —— 被拒的提问不该在库里留下痕迹；
    · 响应体里有没有前端能用来退避的字段（`retry_after_s` / `code`）；
    · token 记账是不是**回答结束之后**才发生、而且记的是真数；
    · Redis 挂了会不会把整站 429 掉（fail-open）。

⚠️ 假客户端与 `_seed_chunk` 从 `test_chat_api` 借 —— 刻意**不复制一份**：
    复制的那一刻，两边的假实现就开始各自漂移，改了一边另一边不报错。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.api.documents import get_enqueue
from app.core.config import get_settings
from app.core.limits import _quota_key, _rate_key
from app.db.models.conversation import Message, MessageRole
from app.db.models.user import UserRole
from tests.fakeredis import FakeRedis
from tests.test_chat_api import UNIT_VECTOR, FakeChat, FakeEmbedder, _seed_chunk

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix
DOC = "# 退换货\n\n无理由退货 7 天。\n"


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------
async def _headers(make_account, login) -> tuple[dict[str, str], uuid.UUID]:
    account = await make_account(role=UserRole.ADMIN)
    token = await login(account.email, account.password)
    return account.auth(token), account.user_id


async def _new_conversation(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(f"{PREFIX}/conversations", json={"title": "限流用例"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _ask(
    client: AsyncClient, headers: dict[str, str], conversation_id: str, text: str = "退货几天？"
):
    return await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        json={"question": text},
        headers=headers,
    )


async def _tenant_of(session: AsyncSession, conversation_id: uuid.UUID) -> uuid.UUID:
    """从会话反查租户（测试里造数据用）。"""
    from app.db.models.conversation import Conversation

    tenant_id = await session.scalar(
        select(Conversation.tenant_id).where(Conversation.id == conversation_id)
    )
    assert tenant_id is not None
    return tenant_id


def _quota_key_for(user_id: uuid.UUID) -> str:
    key, _ = _quota_key(str(user_id), datetime.now(UTC))
    return key


def _rate_key_for(user_id: uuid.UUID, *, offset_seconds: int = 0) -> str:
    """这个用户在某分钟窗口的速率计数键（`offset_seconds` 用来取"下一分钟"）。"""
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    key, _ = _rate_key(str(user_id), moment)
    return key


def _fill_rate_window(redis: FakeRedis, user_id: uuid.UUID, limit: int) -> None:
    """把**本分钟和下一分钟**的计数都铺满。

    ⚠️ 只铺当前分钟是不够的：固定窗口按墙钟切，请求要是落在 `:59.99` 之后，
        就进了下一分钟那个**空的**窗口 —— 断言 429 会偶发失败（踩到过两次）。
        两个都铺掉，测试就与墙钟无关了。
    """
    for offset in (0, 60):
        redis.seed(_rate_key_for(user_id, offset_seconds=offset), limit)


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """上传目录挪到临时目录（上传用例会真的落盘）。"""
    target = tmp_path / "uploads"
    monkeypatch.setattr(get_settings(), "upload_dir", str(target))
    return target


# ------------------------------------------------------------
# 速率：429 的形状与时机
# ------------------------------------------------------------
async def test_rate_limit_returns_actionable_429(
    client: AsyncClient,
    app,
    make_account,
    login,
    limits_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被限时给的是**能照着退避的 429**，不是一句"太频繁"。

    ⚠️ 这里**预先把这个用户本分钟的计数铺满**，而不是"连发两次等它超"。
        连发会 flake：固定窗口按墙钟切分钟 —— 第一次落在 `:59.9`、
        第二次落在下一分钟，两次就各自落进一个新窗口，断言 429 会随机失败
        （第一次跑就踩到了，查了半天才发现是测试自己的时间依赖）。
        铺计数则与时间无关：下一个请求一定是 `limit + 1`。
    """
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 3)
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    headers, user_id = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)
    _fill_rate_window(limits_redis, user_id, 3)

    resp = await _ask(client, headers, conversation_id)

    assert resp.status_code == 429
    body = resp.json()
    # ⚠️ 这几个字段是**前端做退避**的依据，少一个就只能瞎重试
    assert body["code"] == "rate_limited"
    assert body["scope"] == "rate"
    assert body["limit"] == 3
    assert body["used"] == 4
    assert body["retry_after_s"] >= 1
    assert "秒后再试" in body["detail"]
    # 标准头：非我们前端的调用方（脚本 / 监控）也能照着退避
    assert resp.headers["Retry-After"] == str(body["retry_after_s"])


async def test_rate_limited_question_is_not_persisted(
    client: AsyncClient,
    app,
    make_account,
    login,
    session: AsyncSession,
    limits_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被限流的那次请求**不该在库里留下提问**。

    ⚠️ 这不是洁癖：留下的话，会话列表里会出现一堆"问了但没有任何回答"的消息，
        用户还会以为"是不是系统吞了我的问题"。
    """
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 3)
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    headers, user_id = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)
    _fill_rate_window(limits_redis, user_id, 3)

    blocked = await _ask(client, headers, conversation_id, "被拦住的问题")

    assert blocked.status_code == 429
    user_messages = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == uuid.UUID(conversation_id), Message.role == MessageRole.USER
        )
    )
    assert user_messages == 0


async def test_rate_limit_is_per_user_not_global(
    client: AsyncClient, app, make_account, login, limits_redis: FakeRedis
) -> None:
    """一个人被限，不该牵连别人 —— 否则就是"全站一个额度"。"""
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    headers_a, user_a = await _headers(make_account, login)
    headers_b, _ = await _headers(make_account, login)
    conv_a = await _new_conversation(client, headers_a)
    conv_b = await _new_conversation(client, headers_b)
    # 只把 A 的额度铺满，B 保持干净
    _fill_rate_window(limits_redis, user_a, get_settings().rate_limit_per_minute)

    assert (await _ask(client, headers_a, conv_a)).status_code == 429
    # B 还没发过，照常可用
    assert (await _ask(client, headers_b, conv_b)).status_code == 200


# ------------------------------------------------------------
# 配额：真 token 数
# ------------------------------------------------------------
async def test_tokens_are_recorded_from_the_api_usage(
    client: AsyncClient, app, make_account, login, session: AsyncSession, limits_redis: FakeRedis
) -> None:
    """回答结束后，把模型回报的 token 记进当天账上。

    ⚠️ 记的是**模型回报的数**（`FakeChat` 模拟最后一帧的 usage），不是字数估算。
    """
    app.dependency_overrides[get_clients] = lambda: (
        FakeChat(total_tokens=42),
        FakeEmbedder(UNIT_VECTOR),
        None,
    )
    headers, user_id = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)

    # 先塞一条命中率足够高的 chunk，让流程走到"真的调用模型"那一支
    tenant_id = await _tenant_of(session, uuid.UUID(conversation_id))
    await _seed_chunk(
        session, tenant_id=tenant_id, content="无理由退货 7 天。", embedding=UNIT_VECTOR
    )

    resp = await _ask(client, headers, conversation_id)

    assert resp.status_code == 200
    assert limits_redis.data.get(_quota_key_for(user_id)) == 42


async def test_refusal_records_zero_tokens(
    client: AsyncClient, app, make_account, login, limits_redis: FakeRedis
) -> None:
    """拒答**根本没调模型** ⟹ 一分钱不花 ⟹ 账上不该有数。

    ⚠️ 这是"拒答在调模型之前判"（约定 17）的另一个好处：账面上看得出来。
    """
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), FakeEmbedder(UNIT_VECTOR), None)
    headers, user_id = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)

    resp = await _ask(client, headers, conversation_id)

    assert resp.status_code == 200
    assert limits_redis.data.get(_quota_key_for(user_id)) is None


async def test_quota_exhausted_blocks_and_hints_the_reset(
    client: AsyncClient, app, make_account, login, session: AsyncSession, limits_redis: FakeRedis
) -> None:
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    headers, user_id = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)
    # 额度已经用满
    limits_redis.seed(_quota_key_for(user_id), get_settings().token_quota_per_day)

    resp = await _ask(client, headers, conversation_id)

    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "quota_exhausted"
    assert body["scope"] == "quota"
    # 必须说清"什么时候恢复"，否则用户只能干等
    assert "零点" in body["detail"]
    # 也没有落库
    count = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == uuid.UUID(conversation_id), Message.role == MessageRole.USER
        )
    )
    assert count == 0


# ------------------------------------------------------------
# 上传：另一把尺子
# ------------------------------------------------------------
class _RecordingEnqueue:
    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def __call__(self, job: object) -> None:
        self.jobs.append(job)


async def test_upload_rate_limit(
    client: AsyncClient, app, make_account, login, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传比问答严得多（一次上传 = 一遍切分 + N 次 embedding，还占着串行队列）。"""
    monkeypatch.setattr(get_settings(), "upload_rate_limit_per_minute", 1)
    app.dependency_overrides[get_enqueue] = lambda: _RecordingEnqueue()
    headers, _ = await _headers(make_account, login)
    files = {"file": ("退换货.md", DOC.encode(), "text/markdown")}

    first = await client.post(f"{PREFIX}/documents", headers=headers, files=files)
    second = await client.post(f"{PREFIX}/documents", headers=headers, files=files)

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"


# ------------------------------------------------------------
# fail-open
# ------------------------------------------------------------
async def test_chat_still_works_when_redis_is_down(
    client: AsyncClient, app, make_account, login, limits_redis: FakeRedis
) -> None:
    """Redis 挂了 → **放行**，不是把整站 429 掉。

    ⚠️ 这两把尺子治的是成本，不是安全。缓存抖一下就让服务不可用，这个交换不划算。
        （代价要承认：那段时间里没人限流、也没人记账 —— 靠 `gq_limit_failopen_total` 看见。）
    """
    limits_redis.broken = True
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    headers, _ = await _headers(make_account, login)
    conversation_id = await _new_conversation(client, headers)

    resp = await _ask(client, headers, conversation_id)

    assert resp.status_code == 200
