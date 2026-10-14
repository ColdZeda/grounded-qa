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
    created_at: datetime
    updated_at: datetime
