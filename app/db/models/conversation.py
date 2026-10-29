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
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
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


class Channel(enum.StrEnum):
    """会话从哪个渠道来的（走查 P1-⑥，决策 D5）。

    ⚠️ 三种值对应**三种上下文边界**，不是三种"界面皮肤"：
        · `WEB` —— 内部坐席在网页上自己开的会话
        · `FEISHU_P2P` —— 顾客私聊机器人：`chat_id` 天生一人一个 ⟹ 天然隔离
        · `FEISHU_GROUP` —— 顾客在群里问：**同一个 `chat_id` 里有好几个人**
          ⟹ 必须按 `open_id` 再分桶，否则 A 的追问会读到 B 的上下文
    """

    WEB = "web"
    FEISHU_P2P = "feishu_p2p"
    FEISHU_GROUP = "feishu_group"


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

    # 渠道（走查 P1-⑥）
    #: 从哪来。默认 `web` —— 迁移时老数据全是网页端开的会话。
    channel: Mapped[Channel] = mapped_column(
        pg_enum(Channel, "conversation_channel"),
        nullable=False,
        default=Channel.WEB,
        server_default=Channel.WEB.value,
    )
    #: 飞书 `chat_id`（同一个群共用一个）。`web` 为 NULL。
    external_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 飞书 `open_id`（**分桶键**：群里按人分）。`web` 为 NULL。
    #: ⚠️ 只存这一个 id，**头像 / union_id / 手机号 / 部门 / 邮箱一律不存**（用户 2026-10-24 明确）。
    external_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 显示用的真名（群名 / 用户名）。**是缓存**，拿不到就退成 `open_id` 后 6 位。
    external_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 上次解析 `external_title` 的时间 —— 隔 **7 天**再刷一次（群改名能跟上）。
    #: ⚠️ 规格里没写这一列，但"每 7 天刷新"没有它就没法实现（`updated_at` 会被每条消息改掉，不能当依据）。
    #: 会话级便筺（阶段 7 · agent）：**跨轮要记住的事实**（单号 / 商品 / 参照时刻）。
    #:
    #: ⚠️ 为什么不塞进 `messages.meta`：那些是**每一句**的记录，而这个是
    #:    **当前状态**。每轮都从上百条历史里重新推导一遍，既慢又容易推错。
    #: ⚠️ 也不进向量 / 不参与检索 —— 它只是给提示词看的。
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    external_title_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="raise",
    )

    __table_args__ = (
        # `channel` 用 CHECK 而不是 PG 原生 ENUM —— 加取值要 `ALTER TYPE`，见 `AGENTS §7.1` 约定 15
        enum_check("channel", Channel, "channel"),
        # 外部会话的**去重键**：同一租户 + 同一渠道 + 同一群 + 同一个人 = 同一个会话。
        # ⚠️ `web` 那三列都是 NULL，而 PG 的唯一索引**认为 NULL 互不相等** ⟹ 网页端会话
        #    不受这条约束影响（它们靠 `user_id` 分，见 `app/api/conversations.py`）。
        Index(
            "uq_conversations_external",
            "tenant_id",
            "channel",
            "external_chat_id",
            "external_user_id",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} {self.channel.value} {self.title!r}>"


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

    #: 机器附加的"记忆"：这条消息当时有没有带图、图读到了什么、查到哪一单。
    #: 形如 `{"image_id": "...", "image_note": "...", "order_no": "...", "order_snapshot": {...}}`
    #: ⚠️ **绝不塞进 `content`** —— 用户原话和机器补的信息必须能分开：
    #:    混在一起则 ① 排障时分不清哪些是用户说的 ② 前端会把内部字样渲染出来
    #:    ③ 评测拿 `content` 当"用户输入"的口径就失真了。
    #: ⚠️ 追问时靠**它**恢复上下文（模型看不到图），而不是重新调一次读图模型 ——
    #:    这条设计叫"图只对它的那条消息生效，但读数会变成会话记忆"（architecture.md §9.35）。
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages", lazy="raise")

    __table_args__ = (enum_check("role", MessageRole, "message_role"),)

    def __repr__(self) -> str:
        return f"<Message {self.role.value} {self.content[:20]!r}>"
