"""纠错反馈的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.feedback import FeedbackVerdict


class FeedbackIn(BaseModel):
    """提交一条纠错。

    ⚠️ **答案与引用不在这里** —— 由服务端按 `message_id` 现场取快照。
        让前端传，改一行请求就能伪造一条"当时的答案是 xxx"的记录。
    """

    message_id: uuid.UUID
    verdict: FeedbackVerdict
    #: 可选的一句话。`verdict=other` 时必填（接口层校验）。
    note: str | None = Field(default=None, max_length=1000)


class FeedbackOut(BaseModel):
    """一条反馈（清单页用）。**带快照** —— 那时候的答案现在可能已经不一样了。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    user_id: uuid.UUID | None
    verdict: FeedbackVerdict
    note: str | None
    answer_snapshot: str
    citations_snapshot: list[dict[str, object]]
    created_at: datetime
