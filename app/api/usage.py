"""配额与成本页的数据源。

⚠️ **这里不新开一套计量**：token 是 `app/core/limits.py` 在回答结束时写进 Redis 的
（`gq:quota:{user_id}:{YYYYMMDD}`），本模块只**读**它。
"页面上再算一遍"是最容易让两处数字对不上的做法 —— 同一件事只能有一个写入口。

⚠️ **只统计问答消耗的 token**：拒答不调模型 ⟹ 记 0；embedding / rerank 的用量
**没有计入**（它们按次计费、量级完全不同，混进"用户配额"里没法解释）。
这条必须写在页面上，否则"我们花了多少"这个数字会被误读。

⚠️ **数据只留 2 天**（`add_tokens` 里 TTL = 2×86400）⟹ 这个页面只有"今天"和"昨天"，
    **做不了趋势图**。要看趋势得先有落盘（见 `architecture.md` §9.31 的代价表）。

⚠️ **Redis 取不到时 `None`，不是 0** —— 页面显示 `—`。
    "不知道"和"没花"是两件事，混起来会让人以为额度还很足。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, LimitsRedisDep, SessionDep
from app.core.config import get_settings
from app.core.limits import SHANGHAI, peek_channel_usage, peek_quota, peek_rate
from app.db.models.conversation import Channel
from app.db.models.user import User
from app.schemas.usage import ChannelUsage, MemberUsage, TenantUsageOut, UsageOut

router = APIRouter(prefix="/usage", tags=["usage"])

NOTE = (
    "只统计问答消耗的 token（模型回报的真实 usage；拒答不调模型，所以是 0）。"
    "embedding / rerank 按次计费，不在这个数里。"
    "数据存在 Redis，只保留 2 天，所以只能看今天、做不了趋势。"
    "配额按北京时间的自然日切，零点重置。"
    "「按渠道」是把**同一笔总量**拆开看：飞书的消息仍然全部记在渠道账号名下，"
    "不是按人记的。"
)

#: 渠道 → 给人看的名字。⚠️ 键取自 `Channel` 枚举（**不要另抄一份字符串**：
#: 抄一份的后果是枚举改了、页面还在显示旧名字，而且没有任何报错）。
CHANNEL_LABELS: dict[Channel, str] = {
    Channel.WEB: "内部 Web",
    Channel.FEISHU_P2P: "飞书私聊",
    Channel.FEISHU_GROUP: "飞书群聊",
}


def _today(now: datetime) -> str:
    return now.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def _reset_at(now: datetime) -> str:
    """额度重置时间 = 北京时间明天 00:00。"""
    local = now.astimezone(SHANGHAI)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.strftime("%Y-%m-%d %H:%M")


def _cost(tokens: int | None, price: float) -> float | None:
    """估算成本（元）。⚠️ 单价没配（<=0）就返回 `None` —— **不要假装是 0 元**。"""
    if tokens is None or price <= 0:
        return None
    return round(tokens / 1_000_000 * price, 4)


@router.get("/me", response_model=UsageOut, summary="我今天的配额用量")
async def my_usage(
    current_user: CurrentUser,
    redis: LimitsRedisDep,
) -> UsageOut:
    """当前用户今天的用量。**所有登录用户都能看自己的**（这不是运维面）。"""
    settings = get_settings()
    now = datetime.now(SHANGHAI)

    used = await peek_quota(redis, subject=str(current_user.id), now=now)
    rate_used = await peek_rate(redis, subject=str(current_user.id), now=now)
    price = settings.llm_price_per_million_tokens

    per = await peek_channel_usage(
        redis,
        subject=str(current_user.id),
        channels=[channel.value for channel in CHANNEL_LABELS],
        now=now,
    )
    channels = [
        ChannelUsage(channel=channel.value, label=label, tokens_used=per.get(channel.value))
        for channel, label in CHANNEL_LABELS.items()
    ]
    #: 分项之和。**任何一项取不到就整体算 None**（宁可说"不知道"，也不给一个少一块的合计）
    attributed: int | None = (
        None
        if any(item.tokens_used is None for item in channels)
        else sum(item.tokens_used or 0 for item in channels)
    )
    #: 总量里没能归到渠道的部分：加渠道维度之前的账会落在这里，第二天自动归零。
    #: ⚠️ 用 `max(0, …)`：明细比总量多只可能是写入时序造成的毛刺，显示负数只会让人困惑。
    unattributed = None if used is None or attributed is None else max(0, used - attributed)

    return UsageOut(
        date=_today(now),
        tokens_used=used,
        quota_limit=settings.token_quota_per_day,
        tokens_remaining=max(0, settings.token_quota_per_day - (used or 0)),
        requests_this_minute=rate_used,
        rate_limit_per_minute=settings.rate_limit_per_minute,
        reset_at_display=_reset_at(now),
        cost_estimate_cny=_cost(used, price),
        price_per_million_tokens=price if price > 0 else None,
        generated_at_display=now.strftime("%Y-%m-%d %H:%M:%S"),
        note=NOTE,
        channels=channels,
        tokens_unattributed=unattributed,
    )


@router.get("/tenant", response_model=TenantUsageOut, summary="租户内成员今天的配额用量（管理员）")
async def tenant_usage(
    admin: AdminUser,
    session: SessionDep,
    redis: LimitsRedisDep,
) -> TenantUsageOut:
    """**租户内的**管理员看全员用量。

    ⚠️ 只查 `admin.tenant_id` 下的用户 —— 管理员是**租户内**的管理员，不是跨租户超管。
    ⚠️ 逐个用户读 Redis（不打 MGET）：用户数是"一屋子客服"的量级，
        这里多几次往返没关系；换来的是**键的构造只有一处**（`limits._quota_key`）。
    """
    settings = get_settings()
    now = datetime.now(SHANGHAI)

    rows = (
        await session.scalars(
            select(User).where(User.tenant_id == admin.tenant_id).order_by(User.email)
        )
    ).all()

    #: 飞书渠道账号：**所有飞书消息都记在它名下**（私聊＋群聊＋所有群）。
    #: ⚠️ 不加这个标记，成员表里那一行就是个"看不出是谁、数字还特别大"的账号 ——
    #:    用户 2026-10-25 走查时就这么困惑过一次。
    channel_email = (settings.feishu_channel_email or settings.admin_email).strip().lower()

    items: list[MemberUsage] = []
    total: int | None = 0
    for user in rows:
        used = await peek_quota(redis, subject=str(user.id), now=now)
        if used is None:
            total = None  # 有一个取不到，合计就不能装作是准的
        elif total is not None:
            total += used
        items.append(
            MemberUsage(
                email=user.email,
                role=user.role.value,
                tokens_used=used,
                quota_limit=settings.token_quota_per_day,
                is_channel_account=user.email.strip().lower() == channel_email,
            )
        )

    # 用得多的排前面（取不到的排最后 —— 它不是"用得少"，是"不知道"）
    items.sort(key=lambda item: (item.tokens_used is None, -(item.tokens_used or 0)))

    return TenantUsageOut(
        date=_today(now),
        members=len(items),
        tokens_used_total=total,
        items=items,
        generated_at_display=now.strftime("%Y-%m-%d %H:%M:%S"),
        note=NOTE,
    )
