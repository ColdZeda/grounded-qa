"""配额页的返回结构（`app/api/usage.py`）。

⚠️ **不许裸数字**（全项目同一条规矩，见 `app/schemas/report.py`）：
    每个用量都带**上限**（分母）和**统计口径**（哪一天、北京时间、只算问答消耗）；
    Redis 取不到时用 `None`，前端显示 `—` —— **不能显示 0**（0 是"没花"，None 是"不知道"）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class MemberUsage(BaseModel):
    """租户内某个成员今天的用量。"""

    email: str
    role: str
    tokens_used: int | None = None
    quota_limit: int


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
