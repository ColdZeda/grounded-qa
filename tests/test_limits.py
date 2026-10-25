"""限流与 token 配额 —— **纯单元测试**（内存假 Redis，不连库、不联网）。

⚠️ 这里守的是三件容易做错的事：

    1. **窗口对齐**：靠 TTL 当窗口的话，"每 61 秒发一次"永远不会触发限流；
    2. **先加后判**：先判后加的话，实际放行量是上限的两倍；
    3. **fail-open**：Redis 挂了要**放行并留痕**，不是把整站 429 掉。

⚠️ 另外，`retry_after_s` 必须**真的能用**（前端会照着它退避）——
    算错方向（给成"已经过去多久"）会让前端立刻重试，然后把限流刷得更狠。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.limits import (
    LimitDecision,
    RateLimitedError,
    _quota_key,
    _rate_key,
    add_tokens,
    consume_rate,
    ensure,
    human_delay,
    peek_channel_usage,
    peek_quota,
    read_quota,
)
from tests.fakeredis import FakeRedis

SUBJECT = str(uuid.uuid4())


def _at(hour: int, minute: int = 0, second: int = 0, *, day: int = 17) -> datetime:
    """上海时间构造一个时刻（返回带时区的 datetime）。"""
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, day, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai"))


# ------------------------------------------------------------
# 速率
# ------------------------------------------------------------
async def test_rate_allows_up_to_the_limit_then_denies() -> None:
    redis = FakeRedis()

    allowed = [
        await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 0)) for _ in range(3)
    ]
    denied = await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 0))

    assert all(d.allowed for d in allowed)
    assert denied.allowed is False
    # ⚠️ 被拒的那一次**也计数**（先加后判），所以 used 会继续涨
    assert denied.used == 4
    assert denied.limit == 3


async def test_rate_window_is_aligned_to_the_minute_not_to_first_request() -> None:
    """同一分钟内共享计数，跨分钟各自归零。

    ⚠️ 这就是"别拿 TTL 当窗口"的原因：靠 TTL 的话窗口从第一次请求开始算，
        每 61 秒发一次永远不会触发限流。
    """
    redis = FakeRedis()

    for _ in range(3):
        await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 0, 10))

    # 同一分钟的第 11 秒、第 59 秒 —— 都还在同一个窗口里，继续被拒
    assert (
        await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 0, 11))
    ).allowed is False
    assert (
        await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 0, 59))
    ).allowed is False

    # 下一秒就是新窗口了
    assert (await consume_rate(redis, subject=SUBJECT, limit=3, now=_at(10, 1, 0))).allowed is True


async def test_rate_retry_after_counts_down_within_the_minute() -> None:
    redis = FakeRedis()

    # limit=0 → 第一个请求就被拒（这样才有话术可说：放行的决策 `message()` 是空串）
    decision = await consume_rate(redis, subject=SUBJECT, limit=0, now=_at(10, 0, 45))

    # 45 秒处 → 还有 15 秒到下一分钟
    assert decision.allowed is False
    assert decision.retry_after_s == 15
    assert "15 秒" in decision.message()


async def test_rate_is_per_subject() -> None:
    """一个用户被限，不能牵连另一个用户。"""
    redis = FakeRedis()
    other = str(uuid.uuid4())

    await consume_rate(redis, subject=SUBJECT, limit=1, now=_at(10, 0))
    blocked = await consume_rate(redis, subject=SUBJECT, limit=1, now=_at(10, 0))
    fresh = await consume_rate(redis, subject=other, limit=1, now=_at(10, 0))

    assert blocked.allowed is False
    assert fresh.allowed is True


# ------------------------------------------------------------
# 配额
# ------------------------------------------------------------
async def test_quota_is_read_only_and_blocks_at_the_limit() -> None:
    redis = FakeRedis()
    key, _ = _quota_key(SUBJECT, _at(10, 0))

    assert (await read_quota(redis, subject=SUBJECT, limit=100, now=_at(10, 0))).allowed is True

    await add_tokens(redis, subject=SUBJECT, tokens=100, channel="web", now=_at(10, 0))
    decision = await read_quota(redis, subject=SUBJECT, limit=100, now=_at(10, 0))

    # ⚠️ `read_quota` **不写库**：连续读两次，用量不该变（加是在回答之后）
    assert decision.allowed is False
    assert decision.used == 100
    assert decision.code == "quota_exhausted"
    assert redis.data[key] == 100


async def test_quota_accumulates_tokens() -> None:
    redis = FakeRedis()

    await add_tokens(redis, subject=SUBJECT, tokens=30, channel="web", now=_at(10, 0))
    await add_tokens(redis, subject=SUBJECT, tokens=12, channel="web", now=_at(10, 0))

    decision = await read_quota(redis, subject=SUBJECT, limit=1000, now=_at(10, 0))
    assert decision.used == 42


async def test_tokens_are_also_split_by_channel_without_touching_the_quota() -> None:
    """按渠道另记一本明细账，**配额那一笔完全不受影响**（2026-10-25）。

    ⚠️ 这条测试盯两件事：① 三档之和 == 总量；② 加明细**不改变限流判据**。
       哪天有人在 `add_tokens` 里把渠道键当成配额键用，或者漏记一个渠道，
       这两条断言会立刻红 —— 否则表现只是"页面上的数字对不上"，没人会发现。
    """
    redis = FakeRedis()

    await add_tokens(redis, subject=SUBJECT, tokens=100, channel="web", now=_at(10, 0))
    await add_tokens(redis, subject=SUBJECT, tokens=30, channel="feishu_group", now=_at(10, 0))

    per = await peek_channel_usage(
        redis,
        subject=SUBJECT,
        channels=["web", "feishu_p2p", "feishu_group"],
        now=_at(10, 5),
    )

    assert per == {"web": 100, "feishu_p2p": 0, "feishu_group": 30}
    total = await peek_quota(redis, subject=SUBJECT, now=_at(10, 5))
    assert sum(per.values()) == total == 130


async def test_quota_ignores_zero_and_negative_tokens() -> None:
    """拒答是 0 token（根本没调模型）—— 不该因为它写一条 0 进去。"""
    redis = FakeRedis()

    await add_tokens(redis, subject=SUBJECT, tokens=0, channel="web", now=_at(10, 0))
    await add_tokens(redis, subject=SUBJECT, tokens=-5, channel="web", now=_at(10, 0))

    assert redis.data == {}


async def test_quota_retry_after_points_at_next_midnight_beijing_time() -> None:
    """配额的"还要等多久"必须是**北京时间的零点**，不是 UTC 的。

    ⚠️ 按 UTC 切的话，北京用户会在**早上 8 点**被重置 —— 那正是上班时间，
        看起来就像"刚开始用就没额度了"。
    """
    redis = FakeRedis()

    decision = await read_quota(redis, subject=SUBJECT, limit=0, now=_at(23, 30))

    # 23:30 → 到零点还有 30 分钟（+1 秒的取整余量）
    assert 1800 <= decision.retry_after_s <= 1801
    assert "分钟" in decision.message()


async def test_quota_window_rolls_over_at_midnight() -> None:
    redis = FakeRedis()

    await add_tokens(redis, subject=SUBJECT, tokens=100, channel="web", now=_at(23, 59, 59))
    # 同一刻仍算今天
    assert (
        await read_quota(redis, subject=SUBJECT, limit=100, now=_at(23, 59, 59))
    ).allowed is False
    # ⚠️ 跨的是**日期**：第二天 00:01 是新的一天（不是"同一天凌晨"）
    assert (await read_quota(redis, subject=SUBJECT, limit=100, now=_at(0, 1, day=18))).used == 0


# ------------------------------------------------------------
# fail-open
# ------------------------------------------------------------
async def test_redis_down_means_allowed_but_flagged() -> None:
    """Redis 挂了 → 放行，但 `failopen=True`（监控上要看得见）。"""
    redis = FakeRedis(broken=True)

    rate = await consume_rate(redis, subject=SUBJECT, limit=1)
    quota = await read_quota(redis, subject=SUBJECT, limit=1)

    assert rate.allowed is True
    assert rate.failopen is True
    assert quota.allowed is True
    assert quota.failopen is True


async def test_add_tokens_swallows_redis_errors() -> None:
    """记账失败不能让**已经生成完的回答**变成 500 —— 用户已经看到字了。"""
    redis = FakeRedis(broken=True)

    await add_tokens(redis, subject=SUBJECT, tokens=100, channel="web")  # 不抛


# ------------------------------------------------------------
# 话术与异常
# ------------------------------------------------------------
def test_human_delay_uses_the_unit_people_think_in() -> None:
    assert human_delay(1) == "1 秒"
    assert human_delay(59) == "59 秒"
    assert human_delay(60) == "1 分钟"
    assert human_delay(1800) == "30 分钟"
    # ⚠️ 3600 要说成「1 小时」而不是「60 分钟」—— 读起来更省事
    assert human_delay(3600) == "1 小时"
    assert human_delay(7200) == "2 小时"


def test_every_rejection_says_something_actionable() -> None:
    """被拒的消息里**必须**有"等多久"—— 只说"太频繁"等于让用户瞎试。"""
    rate = LimitDecision(allowed=False, scope="rate", used=31, limit=30, retry_after_s=12)
    quota = LimitDecision(
        allowed=False, scope="quota", used=200001, limit=200000, retry_after_s=3600
    )

    assert "12 秒" in rate.message()
    assert "小时" in quota.message()
    assert rate.code == "rate_limited"
    assert quota.code == "quota_exhausted"


def test_ensure_raises_with_a_machine_readable_payload() -> None:
    decision = LimitDecision(allowed=False, scope="rate", used=31, limit=30, retry_after_s=7)

    with pytest.raises(RateLimitedError) as caught:
        ensure(decision)

    payload = caught.value.payload()
    # 前端要靠这几个字段做退避；只给一句话就退不了
    assert payload["code"] == "rate_limited"
    assert payload["retry_after_s"] == 7
    assert payload["used"] == 31
    assert payload["limit"] == 30


def test_ensure_passes_through_when_allowed() -> None:
    decision = LimitDecision(allowed=True, scope="rate", used=1, limit=30, retry_after_s=59)

    assert ensure(decision) is decision


# ------------------------------------------------------------
# 键的构造（白盒，但值得钉住）
# ------------------------------------------------------------
def test_rate_key_carries_the_aligned_minute() -> None:
    key, remaining = _rate_key(SUBJECT, _at(10, 7, 20))

    assert key.endswith("202610171007")
    assert remaining == 40


def test_rate_key_tolerates_utc_input() -> None:
    """传进来的 `now` 可能是 UTC（`datetime.now(UTC)`）—— 键必须是**北京时间**的日期。"""
    utc_moment = datetime(2026, 10, 17, 16, 0, tzinfo=UTC)  # == 北京 10-18 00:00

    key, _ = _rate_key(SUBJECT, utc_moment)

    assert key.endswith("202610180000")


def test_quota_key_is_namespaced_apart_from_rate_keys() -> None:
    """两个键空间不能撞 —— 撞了就是"限流读到了配额的数"。"""
    rate_key, _ = _rate_key(SUBJECT, _at(10, 0))
    quota_key, _ = _quota_key(SUBJECT, _at(10, 0))

    assert rate_key.startswith("gq:rl:")
    assert quota_key.startswith("gq:quota:")
    assert rate_key != quota_key
