"""订单通道：**租户内可查** ＋ 单号抽取 ＋ 事实卡片。

为什么这些用例值得单独写：

    订单是本项目里**唯一带"谁的数据"味道**的业务表。它的隔离口径在 2026-10-24
    因决策 D5 改过一次（"用户私有" → "租户内可查"，Web 端是内部坐席工作台）。
    这类口径最容易在重构中**悄悄回退**，而且回退时**接口照样 200、页面照样好看** ——
    所以两条线都用断言钉住：① 同租户同事能查（坐席视角）② **跨租户必须 404**。

另外两条与模型行为有关的口径也在这里钉：
    ① 单号抽取**只按形态捞**，真伪交给数据库（不是靠正则猜格式）
    ② `order_snapshot` 里**只许放事实**，不许出现"能不能退"这类结论 ——
       否则模型会把我们的判断当规则复述
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TEST_BCRYPT_ROUNDS, TEST_PASSWORD, Account
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models.order import Order, OrderStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.orders import (
    days_since_delivery,
    extract_order_candidates,
    order_snapshot,
    render_order_facts,
)

PREFIX = get_settings().api_prefix


def _now() -> datetime:
    return datetime.now(UTC)


async def _add_order(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    order_no: str = "SF90001",
    status: OrderStatus = OrderStatus.DELIVERED,
    delivered_days_ago: int | None = 5,
) -> Order:
    order = Order(
        tenant_id=tenant_id,
        user_id=user_id,
        order_no=order_no,
        status=status,
        placed_at=_now() - timedelta(days=10),
        delivered_at=(
            None if delivered_days_ago is None else _now() - timedelta(days=delivered_days_ago)
        ),
        items=[{"name": "测试商品", "qty": 1}],
        amount_cents=123_400,
        currency="CNY",
    )
    session.add(order)
    await session.commit()
    return order


# ---------------------------------------------------------------- 接口：越权面


async def test_owner_reads_own_order(
    client: AsyncClient,
    session: AsyncSession,
    make_account: Callable[..., Awaitable[Account]],
    login: Callable[[str, str], Awaitable[str]],
) -> None:
    account: Account = await make_account(role=UserRole.ADMIN, label="own")
    await _add_order(session, tenant_id=account.tenant_id, user_id=account.user_id)
    token = await login(account.email, account.password)

    resp = await client.get(f"{PREFIX}/orders/SF90001", headers=account.auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_no"] == "SF90001"
    assert body["status"] == "delivered"
    assert body["amount_cents"] == 123_400
    assert body["items"][0]["name"] == "测试商品"


async def test_order_no_is_case_insensitive(
    client: AsyncClient,
    session: AsyncSession,
    make_account: Callable[..., Awaitable[Account]],
    login: Callable[[str, str], Awaitable[str]],
) -> None:
    account: Account = await make_account(label="case")
    await _add_order(session, tenant_id=account.tenant_id, user_id=account.user_id)
    token = await login(account.email, account.password)

    resp = await client.get(f"{PREFIX}/orders/sf90001", headers=account.auth(token))
    assert resp.status_code == 200, resp.text


async def test_same_tenant_colleague_can_read_order(
    client: AsyncClient, session: AsyncSession, login: Callable[[str, str], Awaitable[str]]
) -> None:
    """同租户的另一位坐席**能**查到这张单 —— 这是决策 D5 的结果，不是漏了过滤。

    ⚠️ 2026-10-24 之前这条是 `assert 404`（"订单是用户私有"）。Web 端定为
       **内部坐席工作台**之后，口径变成"**租户内可查**"：坐席手里只有顾客报的单号，
       只能查自己的单等于这个功能没有。
       真正的隔离线是**租户**（下一条用例钉的就是它）。
    """
    tenant = Tenant(slug=f"share-{uuid.uuid4().hex[:8]}", name="共享租户")
    session.add(tenant)
    await session.flush()
    try:

        def _mk(label: str) -> User:
            return User(
                tenant_id=tenant.id,
                email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(TEST_PASSWORD, rounds=TEST_BCRYPT_ROUNDS),
                role=UserRole.ADMIN,
                is_active=True,
            )

        owner, colleague = _mk("owner"), _mk("colleague")
        session.add_all([owner, colleague])
        await session.commit()

        await _add_order(session, tenant_id=tenant.id, user_id=owner.id, order_no="SF91001")

        # 同事（不是下单人）也能查到 —— 内部坐席视角
        token = await login(colleague.email, TEST_PASSWORD)
        resp = await client.get(
            f"{PREFIX}/orders/SF91001", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["order_no"] == "SF91001"

        # 下单人自己当然也看得到
        owner_token = await login(owner.email, TEST_PASSWORD)
        ok = await client.get(
            f"{PREFIX}/orders/SF91001", headers={"Authorization": f"Bearer {owner_token}"}
        )
        assert ok.status_code == 200, ok.text
    finally:
        await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()


async def test_other_tenant_gets_404(
    client: AsyncClient,
    session: AsyncSession,
    make_account: Callable[..., Awaitable[Account]],
    login: Callable[[str, str], Awaitable[str]],
) -> None:
    owner: Account = await make_account(label="t-a")
    other: Account = await make_account(label="t-b")
    await _add_order(session, tenant_id=owner.tenant_id, user_id=owner.user_id, order_no="SF92001")

    token = await login(other.email, other.password)
    resp = await client.get(f"{PREFIX}/orders/SF92001", headers=other.auth(token))
    assert resp.status_code == 404, resp.text


async def test_unknown_order_is_404(
    client: AsyncClient,
    make_account: Callable[..., Awaitable[Account]],
    login: Callable[[str, str], Awaitable[str]],
) -> None:
    account: Account = await make_account(label="none")
    token = await login(account.email, account.password)
    resp = await client.get(f"{PREFIX}/orders/SF00000", headers=account.auth(token))
    assert resp.status_code == 404, resp.text


async def test_orders_require_auth(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/orders/SF10001")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------- 单号抽取


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("订单号：SF10001", ["SF10001"]),
        ("sf10001 这单到哪了", ["SF10001"]),
        ("我的单 202610210001 呢", ["202610210001"]),
        ("SF10001 和 SF10002 都查一下", ["SF10001", "SF10002"]),
        # 不该被当成单号的东西
        ("2026-10-17 签收，金额 1,280.00", []),
        ("错误码 E1024", []),
        ("", []),
    ],
)
def test_extract_order_candidates(text: str, expected: list[str]) -> None:
    assert extract_order_candidates(text) == expected


def test_extract_candidates_is_capped() -> None:
    """一次最多看 5 个候选 —— 一串可疑数字不该变成十几次 SQL。"""
    text = " ".join(f"SF{10000 + i}" for i in range(20))
    assert len(extract_order_candidates(text)) == 5


# ---------------------------------------------------------------- 事实卡片


def _order(status: OrderStatus = OrderStatus.DELIVERED) -> Order:
    return Order(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        order_no="SF10007",
        status=status,
        placed_at=_now() - timedelta(days=60),
        delivered_at=_now() - timedelta(days=45),
        items=[{"name": "实木床架", "qty": 1}],
        amount_cents=128_000,
        currency="CNY",
    )


def test_days_since_delivery_counts_calendar_days() -> None:
    order = _order()
    assert days_since_delivery(order, now=_now()) == 45


#: 生产实算的那一对时刻（走查-第二次 2026-10-26 16:03Z 抓到的）。
#: ⚠️ 这两个时间**时分秒不同**，正是它把"24 小时整日"和"自然日"区分开的。
_DELIVERED = datetime(2026, 10, 17, 17, 5, 14, tzinfo=UTC)


def _delivered_order() -> Order:
    order = _order()
    order.delivered_at = _DELIVERED
    return order


def test_days_since_delivery_uses_calendar_days_not_24_hour_windows() -> None:
    """⭐ **生产实算那一例**：10-17 17:05 签收，10-26 16:03 提问。

    24 小时整日 = 8（**错**），自然日 = 9（**对**）。
    边界日上这 1 天会直接把"还能退"答成"不能退"。
    """
    now = datetime(2026, 10, 26, 16, 3, 0, tzinfo=UTC)

    assert days_since_delivery(_delivered_order(), now=now) == 9


def test_days_since_delivery_boundaries_around_the_seven_day_window() -> None:
    """`7 个自然日` 的窗口边界：签收当天 = 第 0 天，第 7 天还能退，第 8 天不能。

    ⚠️ 这里刻意让**时刻**偏早（09:00），若按 24 小时算会少一天 —— 正是要守住的那条缝。
    """
    order = _delivered_order()

    same_day = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)
    day7 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    day8 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)

    assert days_since_delivery(order, now=same_day) == 0
    assert days_since_delivery(order, now=day7) == 7
    assert days_since_delivery(order, now=day8) == 8


def test_days_since_delivery_never_goes_negative() -> None:
    """时钟漂移 / 数据写错导致"未来签收"时给 0，不要给负数（提示词里会很难看）。"""
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert days_since_delivery(_delivered_order(), now=now) == 0


def test_days_since_delivery_is_none_when_not_delivered() -> None:
    order = _order(status=OrderStatus.SHIPPED)
    order.delivered_at = None
    assert days_since_delivery(order, now=_now()) is None


def test_snapshot_has_facts_only() -> None:
    """⚠️ 快照里**只许有事实**。

    这条断言看着啰嗦，但它守的是框架：一旦有人往快照里加"可以退"/"超期"这类**判断**，
    模型就会把我们的话当规则复述 —— 那是把幻觉治理从"检索+条款"退化成"提示词里的一句断言"。
    """
    snapshot = order_snapshot(_order(), now=_now())
    assert snapshot["order_no"] == "SF10007"
    assert snapshot["status"] == "delivered"
    assert snapshot["days_since_delivery"] == 45
    assert snapshot["amount"] == "¥1,280.00"

    rendered = render_order_facts([snapshot])
    for forbidden in ("可以退", "不能退", "超期", "建议", "应该", "转保修"):
        assert forbidden not in rendered, f"事实卡片里出现了判断性措辞：{forbidden}"
    assert "SF10007" in rendered
    assert "已签收" in rendered


def test_render_order_facts_empty_when_no_orders() -> None:
    """⚠️ 查不到订单时**返回空串**（不是"没查到"），这样提示词里那块直接不出现。"""
    assert render_order_facts([]) == ""


async def test_resolve_orders_keeps_candidate_order(session: AsyncSession) -> None:
    """多条命中时按**原文出现顺序**返回，避免同样的输入产生不同的提示词。"""
    tenant = Tenant(slug=f"ord-{uuid.uuid4().hex[:8]}", name="订单租户")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"o-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password(TEST_PASSWORD, rounds=TEST_BCRYPT_ROUNDS),
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    try:
        await _add_order(session, tenant_id=tenant.id, user_id=user.id, order_no="SF93002")
        await _add_order(session, tenant_id=tenant.id, user_id=user.id, order_no="SF93001")

        from app.orders import resolve_orders

        found = await resolve_orders(
            session,
            tenant_id=tenant.id,
            text="先看 SF93001，再看 SF93002",
        )
        assert [row.order_no for row in found] == ["SF93001", "SF93002"]

        # 换一个租户就捞不到（隔离线是租户，不是"是不是下单人"）
        other_tenant = Tenant(slug=f"oth-{uuid.uuid4().hex[:8]}", name="另一个租户")
        session.add(other_tenant)
        await session.commit()
        empty = await resolve_orders(
            session,
            tenant_id=other_tenant.id,
            text="先看 SF93001，再看 SF93002",
        )
        assert empty == []
        await session.execute(delete(Tenant).where(Tenant.id == other_tenant.id))
    finally:
        await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()
