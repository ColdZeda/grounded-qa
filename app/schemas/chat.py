"""消息与问答的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.conversation import MessageRole


class AskRequest(BaseModel):
    """提问。

    ⚠️ `question` 设了长度上限：**这是成本控制的第一道闸**。
        一个 10 万字的"问题"会直接把 prompt 撑爆并把 token 账单拉满，
        而且它根本不是个问题。真实需求（贴一大段日志问为什么报错）交给将来的附件上传。
    """

    question: str = Field(min_length=1, max_length=2000)
    #: 这次提问走哪条路。**`None` = 跟用户偏好 / 默认「快速」**。
    #: ⚠️ 这是“这次提问”的属性，不是身份属性 —— 所以**以请求为准**，用户表只记“上次选了啥”。
    #: ⚠️ **还要与 `open_agent_mode` 相与**：总闸关了时，这里传 `agent` 也不生效（见 `chat.py::_resolve_mode`）。
    mode: Literal["fast", "agent"] | None = None
    #: 可选：先 `POST /api/v1/images` 拿到 `image_id`，再在这里带上它提问。
    #: ⚠️ **图不塞进这个 JSON**（base64 会撑爆请求体、还会进访问日志）；
    #:    走"先上传拿 id、再引用"两步。
    #: ⚠️ 图只对**这一条消息**生效：后续追问靠历史里的文字记忆，不重调读图模型。
    image_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    """一条消息（含引用溯源信息）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    #: `[{"index": 1, "chunk_id": "...", "document_id": "...", "score": 0.87,
    #:    "score_kind": "rerank", "snippet": "..."}]`
    #: ⚠️ **空列表现在只有一种含义了：用户消息。**
    #:    **拒答也会带 `citations`**（2026-10-15 定，见 `architecture.md` §9.19）——
    #:    那时它们是"检索到了、但都不够好"的**候选**，而不是"引用"。
    #:    （这条注释以前写的是"空列表有两种含义：用户消息、以及拒答"，已作废。）
    citations: list[dict[str, object]]
    refused: bool
    #: 置信度（`rerank` 分数，或退化时的**向量余弦**）—— 口径跟着 `citations[].score_kind`。
    #: ⚠️ 用户消息恒为 `None`；助手消息在"**只有全文命中**"时也是 `None`
    #:    （那时没有可信的置信度，见 `architecture.md` §5.2）。
    confidence: float | None
    latency_ms: int | None
    #: 机器附加的记忆（这张图读到了什么、查到哪一单）。用户消息才可能有。
    #: ⚠️ 和 `content` 分开：`content` 永远是**用户原话**，这里是我们补的。
    meta: dict[str, object] | None = None
    created_at: datetime
