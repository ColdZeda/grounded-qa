"""答案纠错反馈（内部坐席用）。

⚠️ **它不是"点赞/点踩"，也不是评分** —— 它是 **badcase 的入口**：
    坐席看出"这条答错了 / 引错了"，点一下就把**当时的答案与引用原样**存下来。

⚠️ 为什么**必须存快照**（`answer_snapshot` / `citations_snapshot`）：
    答案本身可能会被删、知识库块会被替换、阈值会调。
    三个月后回头看这条 badcase，如果只剩一句"这里答错了"，那就等于没有记录。

⚠️ 不进向量、不参与检索、不影响答案 —— 它只是一张待处理的清单。
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum


class FeedbackVerdict(enum.StrEnum):
    """哪里不对。**只分三类** —— 再多就没人肯填了。"""

    WRONG_ANSWER = "wrong_answer"  # 答案本身错了（事实 / 结论不对）
    WRONG_CITATION = "wrong_citation"  # 答案对但引用引错了（串号）
    OTHER = "other"  # 其他（必须写一句）


class AnswerFeedback(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """一条纠错反馈。**同一个人对同一条消息只能有一条**（重复提交是覆盖）。"""

    __tablename__ = "answer_feedback"
    __table_args__ = (
        # 同一用户 + 同一消息唯一：避免一个人点五次就变成五条 badcase。
        # ⚠️ 用数据库约束，**不用"先查后写"** —— 并发下查与写之间有窗口。
        UniqueConstraint("message_id", "user_id", name="feedback_unique_per_user_message"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        # ⚠️ 用户被删了不该把反馈一起带走 —— badcase 本身仍然有价值。
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    verdict: Mapped[FeedbackVerdict] = mapped_column(
        pg_enum(FeedbackVerdict, "feedback_verdict"), nullable=False
    )
    #: 坐席自己写的一句话。`OTHER` 时必填（在接口层校验）。
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 当时的答案原文。**快照，不是引用** —— 后面改了也不影响这条记录。
    answer_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    #: 当时的引用列表（`Citation.as_dict()` 的原样）。
    citations_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )


__all__ = ["AnswerFeedback", "FeedbackVerdict"]
