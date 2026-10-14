"""消息与问答的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.conversation import MessageRole


class AskRequest(BaseModel):
    """提问。

    ⚠️ `question` 设了长度上限：**这是成本控制的第一道闸**。
        一个 10 万字的"问题"会直接把 prompt 撑爆并把 token 账单拉满，
        而且它根本不是个问题。真实需求（贴一大段日志问为什么报错）交给将来的附件上传。
    """

    question: str = Field(min_length=1, max_length=2000)


class MessageOut(BaseModel):
    """一条消息（含引用溯源信息）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    #: `[{"index": 1, "chunk_id": "...", "document_id": "...", "score": 0.87,
    #:    "score_kind": "rerank", "snippet": "..."}]`
    #: ⚠️ 空列表有两种含义：**用户消息**、以及**拒答**（拒答没有依据可引）。
    #:    要区分它们得看 `refused`。
    citations: list[dict[str, object]]
    refused: bool
    latency_ms: int | None
    created_at: datetime
