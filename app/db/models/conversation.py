"""会话与消息。

⚠️ **`citations` 与 `refused` 是本项目的深度锚（引用溯源与幻觉治理）落在数据模型上的样子。**
    这两列现在就要有，而不是等"生成模块做完再加"：
    它们决定了前端能渲染什么、评测能断言什么。
    - `citations`：这条回答引用了哪几个 chunk、各自得分多少 → 前端可点开溯源
      ⚠️ **拒答时这一列也可能不是空的**（见 `architecture.md` §9.19）：
         那时它装的是"检索到了、但都不够好"的**候选** ——
         概念上是"候选"而不是"引用"，**字段名是将就的**（记在 §10.3）。
    - `refused`：这条回答是"证据不足拒答"而不是正常作答 → **拒答必须可被统计**
      （拒答率是幻觉治理的核心指标之一，混在普通回答里就统计不出来了）
    - `confidence`：**拒答是在哪个分数上做出的判断** → 阈值调优与 badcase 复盘都要它。
      不落库的后果是：每次拒答都只说"我不知道"，**却不留下任何"我为什么不知道"的证据**。
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_check,
    pg_enum,
)


class MessageRole(enum.StrEnum):
    """消息角色。`SYSTEM` 目前不落库（提示词在代码里），先留着以备对话历史回放。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Conversation(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """一次连续对话。

    ⚠️ `user_id` 也要带 —— 租户内还要分"是谁的会话"。
        只靠 `tenant_id` 的话，同租户的普通成员能互相看到会话。
        **隔离是两层的：租户之间 + 租户内部按用户。**
    """

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} {self.title!r}>"


class Message(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """会话里的一条消息。

    保留 `updated_at`（而不是只用 `created_at`）是**有意的**：
    流式回答期间会先落一行再逐步补齐（`citations` 在流结束后回填、`refused` 最后才定），
    所以这一行**确实会变**。
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(pg_enum(MessageRole, "message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 引用溯源：`[{"chunk_id": "...", "document_id": "...", "score": 0.87, "snippet": "..."}]`
    # ⚠️ 用 JSONB 而不是另开一张 `message_citations` 表：
    #    引用是"消息的一部分"，从不单独查询，也不跨消息聚合 —— 拆表只增加 JOIN 成本。
    #    （若将来要统计"哪些文档最常被引用"，那时再迁移也不迟。）
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    # 低置信拒答标记 —— 见模块 docstring
    refused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 置信度（`rerank` 分数，或退化时的向量余弦 —— 口径跟着 `citations[].score_kind` 走）。
    #: ⚠️ **用户消息恒为 NULL**（它没有置信度可言）；
    #:    助手消息在"只有全文命中"时也是 NULL（那时**没有可信的置信度**，见 §5.2）。
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 端到端耗时（毫秒）。可观测性要求：没有这个数就没法谈"答辩体验"。
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages", lazy="raise")

    __table_args__ = (enum_check("role", MessageRole, "message_role"),)

    def __repr__(self) -> str:
        return f"<Message {self.role.value} {self.content[:20]!r}>"
