"""配额页的返回结构（`app/api/usage.py`）。

⚠️ **不许裸数字**（全项目同一条规矩，见 `app/schemas/report.py`）：
    每个用量都带**上限**（分母）和**统计口径**（哪一天、北京时间、只算问答消耗）；
    Redis 取不到时用 `None`，前端显示 `—` —— **不能显示 0**（0 是"没花"，None 是"不知道"）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelUsage(BaseModel):
    """某个渠道今天花了多少（**明细账**，不参与限流判定）。

    ⚠️ 三个渠道是**同一笔总量**的三个分项：`web` / `feishu_p2p` / `feishu_group`。
       全加起来的对得上 `UsageOut.tokens_used` 才是对的（有测试盯这条）。
    """

    #: 机器值：`web` / `feishu_p2p` / `feishu_group`（= `Channel` 枚举的值）
    channel: str
    #: 给人看的名字（**颜色之外一定要有文字**，色弱用户和打印稿都要能读）
    label: str
    #: 今天这个渠道消耗的 token；`None` = 取不到
    tokens_used: int | None = None


class UsageOut(BaseModel):
    """**我**今天的用量。"""

    #: 统计日期（北京时间 `YYYY-MM-DD`）—— 配额按北京时间的自然日切
    date: str
    #: 今天已消耗的 token（模型回报的 `usage.total_tokens` 累加）。
    #: `None` = 取不到（Redis 不可用），前端显示 `—`
    tokens_used: int | None = None
    #: 每天的上限
    quota_limit: int
    #: 剩余。**不会被截到 0 以下**（超了就是超了，显示 0 会让"超了多少"看不见）
    tokens_remaining: int = 0
    #: 本分钟已经发了几次问答（限流窗口内）
    requests_this_minute: int | None = None
    #: 每分钟上限
    rate_limit_per_minute: int
    #: 额度什么时候重置（北京时间明天 00:00）
    reset_at_display: str
    #: 估算成本（元）。**只有配了单价才有**（`None` = 没配，页面显示"未配单价"）
    cost_estimate_cny: float | None = None
    #: 本次估算用的单价（元 / 百万 token）—— 不写出来的成本数字不可信
    price_per_million_tokens: float | None = None
    #: 生成时间（北京时间）
    generated_at_display: str
    #: 口径说明（前端原样显示）
    note: str
    #: **按渠道拆**（Web / 飞书私聊 / 飞书群聊）。顺序固定，永远三项。
    #:
    #: ⚠️ **不许再加"未归因 / 差额"之类的字段**（2026-10-25 用户要求删干净）：
    #:    那是我们**迁移期**才知道的信息（"加渠道维度之前记的账"），对用户零价值，
    #:    摆在页面上只会显得草台。**一致性由测试守**（`sum(channels) == tokens_used`），
    #:    不该靠界面上写一行字。
    channels: list[ChannelUsage] = Field(default_factory=list)


class MemberUsage(BaseModel):
    """租户内某个成员今天的用量。"""

    email: str
    role: str
    tokens_used: int | None = None
    quota_limit: int
    #: 这一行是不是**飞书渠道账号**（所有飞书消息都记在它名下，私聊＋群聊）
    is_channel_account: bool = False


class TenantUsageOut(BaseModel):
    """**管理员**看的：租户内所有成员。"""

    date: str
    #: 成员数（= 下表行数）
    members: int
    #: 全租户今天合计（只加取得到的那些；一个都取不到时是 `None`）
    tokens_used_total: int | None = None
    items: list[MemberUsage] = Field(default_factory=list)
    generated_at_display: str
    note: str
