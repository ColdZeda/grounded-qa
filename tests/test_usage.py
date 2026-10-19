"""配额页 —— 记账读取 ＋ 两个接口。

守三件事：
· **只读不改**：`peek_quota` / `peek_rate` 不许有副作用（配额页不该把"被拒次数"刷上去）
· **取不到就是 `—`，不是 0**（Redis 挂了 ≠ 没花额度）
· 租户视图**只含自己租户**的人（管理员是租户内的，不是跨租户超管）
"""

from __future__ import annotations

import pytest

from app.api.usage import _cost, _reset_at
from app.core.config import get_settings
from app.core.limits import add_tokens, peek_quota, peek_rate
from app.db.models.user import UserRole
from tests.fakeredis import FakeRedis

PREFIX = get_settings().api_prefix


# ------------------------------------------------------------
# 记账的只读读取
# ------------------------------------------------------------
async def test_peek_quota_is_none_when_redis_is_down() -> None:
    """Redis 挂了要说"不知道"，**不能返回 0** —— 0 会让页面显示"额度还很足"。"""
    assert await peek_quota(FakeRedis(broken=True), subject="u") is None


async def test_peek_quota_zero_when_untouched() -> None:
    assert await peek_quota(FakeRedis(), subject="u") == 0


async def test_peek_quota_reads_what_add_tokens_wrote() -> None:
    redis = FakeRedis()
    await add_tokens(redis, subject="u1", tokens=1500)
    assert await peek_quota(redis, subject="u1") == 1500
    # 别人不受影响
    assert await peek_quota(redis, subject="u2") == 0


async def test_peek_rate_does_not_increment() -> None:
    """⚠️ 只读：看一眼配额页不该消耗掉一次限流额度。"""
    redis = FakeRedis()
    assert await peek_rate(redis, subject="u") == 0
    assert [call for call, _key in redis.calls] == ["get"]


# ------------------------------------------------------------
# 纯函数
# ------------------------------------------------------------
def test_cost_is_none_without_price() -> None:
    """没配单价就**不给数字** —— 假装 0 元会让"成本"这个说法失去意义。"""
    assert _cost(1_000_000, 0.0) is None
    assert _cost(None, 2.0) is None
    assert _cost(1_000_000, 2.0) == 2.0
    assert _cost(500_000, 2.0) == 1.0


def test_reset_at_is_next_midnight_beijing() -> None:
    from datetime import datetime

    from app.core.limits import SHANGHAI

    # 20:30 的下一零点 = 次日 00:00
    assert _reset_at(datetime(2026, 10, 19, 20, 30, tzinfo=SHANGHAI)) == "2026-10-20 00:00"


# ------------------------------------------------------------
# 接口（需要真库：鉴权要查用户表）
# ------------------------------------------------------------
@pytest.mark.integration
async def test_my_usage_reflects_recorded_tokens(client, login, make_account, limits_redis) -> None:
    account = await make_account(label="usage")
    await add_tokens(limits_redis, subject=str(account.user_id), tokens=12_345)
    token = await login(account.email, account.password)

    response = await client.get(f"{PREFIX}/usage/me", headers=account.auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tokens_used"] == 12_345
    assert body["quota_limit"] == get_settings().token_quota_per_day
    assert body["tokens_remaining"] == body["quota_limit"] - 12_345
    # 默认没配单价 ⟹ 不给成本数字，也不给单价
    assert body["cost_estimate_cny"] is None
    assert body["price_per_million_tokens"] is None
    # 「不许裸数字」：口径说明必须在
    assert "只统计" in body["note"]
    assert body["reset_at_display"]


@pytest.mark.integration
async def test_my_usage_clamps_remaining_but_keeps_used(
    client, login, make_account, limits_redis
) -> None:
    """超额时 `remaining` 贴 0，但 `tokens_used` **保持真实值**（不能把它抹成上限）。"""
    account = await make_account(label="usage-over")
    limit = get_settings().token_quota_per_day
    await add_tokens(limits_redis, subject=str(account.user_id), tokens=limit + 5_000)
    token = await login(account.email, account.password)

    body = (await client.get(f"{PREFIX}/usage/me", headers=account.auth(token))).json()

    assert body["tokens_used"] == limit + 5_000
    assert body["tokens_remaining"] == 0


@pytest.mark.integration
async def test_tenant_usage_is_scoped_and_admin_only(
    client, login, make_account, limits_redis
) -> None:
    """管理员只看到**自己租户**的人；普通成员进不来。"""
    admin = await make_account(role=UserRole.ADMIN, label="usage-admin")
    outsider = await make_account(role=UserRole.MEMBER, label="usage-outsider")
    await add_tokens(limits_redis, subject=str(admin.user_id), tokens=800)
    await add_tokens(limits_redis, subject=str(outsider.user_id), tokens=900)

    admin_token = await login(admin.email, admin.password)
    response = await client.get(f"{PREFIX}/usage/tenant", headers=admin.auth(admin_token))
    assert response.status_code == 200, response.text
    body = response.json()

    emails = [item["email"] for item in body["items"]]
    assert admin.email in emails
    # ⚠️ 另一个租户的成员**绝不能**出现在这里（哪怕他用量更高）
    assert outsider.email not in emails
    assert body["members"] == len(emails)
    assert body["tokens_used_total"] == 800

    member_token = await login(outsider.email, outsider.password)
    denied = await client.get(f"{PREFIX}/usage/tenant", headers=outsider.auth(member_token))
    assert denied.status_code == 403
