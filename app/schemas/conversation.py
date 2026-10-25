"""会话相关的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    """新建会话。

    ⚠️ 请求体里**没有 `tenant_id`、也没有 `user_id`** —— 这是刻意的（见
        `app/db/repository.py` 的 `build()`）：归属由**服务端从 token 推导**，
        绝不接受客户端指定。客户端能指定归属 = 隔离形同虚设。
    """

    title: str = Field(default="新会话", min_length=1, max_length=200)


class ConversationOut(BaseModel):
    """会话列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    user_id: uuid.UUID
    #: 渠道（`web` / `feishu_p2p` / `feishu_group`）—— 界面按它分组 + 上三色标签。
    channel: str
    #: 飞书侧的原生 id 与真名。`web` 的三列都是 NULL。
    #: ⚠️ 前端**只读**，别拿它做权限判断（权限只看租户 + `user_id`）。
    external_chat_id: str | None = None
    external_user_id: str | None = None
    external_title: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationGroupOut(BaseModel):
    """会话列表的一"段"（内部 / 私聊 / 某个群）的**规模信息**。

    ⚠️ 存在的理由（2026-10-25）：以前一次拉全局 50 条再在前端分组，
        **一个组会把配额吃光** —— 内部会话有 200 条时，飞书组一条都不出现。
        现在首屏按组各拉 10 条，靠这个接口知道每组**一共多少条**。
    """

    #: 分组的键：`web` / `p2p` / `grp:{chat_id}`（前端直接当 `key` 用）
    key: str
    #: 机器可读的渠道，前端据此上三色标签
    channel: str
    #: 群里才有；私聊 / 网页是 `None`
    external_chat_id: str | None = None
    #: 群名（拿不到就是 `None`，前端退成 id 后 6 位）
    external_title: str | None = None
    #: 这一组一共多少条（**不是**"已加载多少条"）
    count: int
    #: 这一组最近一次活动时间 —— 用来给群排序（活跃的在前）
    latest_at: datetime


class ConversationSummaryOut(BaseModel):
    """会话列表的分组摘要（首屏只调这一个接口就能画出所有组头）。"""

    #: 全部可见会话的总条数（各 `groups[].count` 之和）
    total: int
    groups: list[ConversationGroupOut] = Field(default_factory=list)
