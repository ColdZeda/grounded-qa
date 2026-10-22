"""知识库：文档与分块。

⚠️ **`chunks.embedding` 的维度来自配置（`EMBEDDING_DIM`），不是写死的常量。**
    但**一旦建了表，这个维度就冻结了** —— 改它必须全量重嵌入
    （AGENTS §7.1 约定 3：换 embedding 模型不报错，只会让检索质量静默变差）。

⚠️ **`embedding_model` / `embedding_dim` 是"元数据锁"的两半**：
    光有向量、不知道它是哪个模型哪一维算出来的，等于没有。
    入库时两者与配置不一致 → 直接拒绝，而不是"先存着以后再说"。
"""

from __future__ import annotations

import enum
import uuid
from datetime import date

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import (
    Base,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_check,
    pg_enum,
)

# 向量维度：读配置（单一事实来源）。改这里 = 必须重建整个向量列 + 全量重嵌入。
EMBEDDING_DIM = get_settings().embedding_dim


class ContentStatus(enum.StrEnum):
    """文档的**内容有效性** —— 和上面的 `DocumentStatus`（入库流程状态）是**两件事**。

    ⚠️ v1.3 的核心：**相关度 relevance ≠ 有效性 eligibility**。
       向量余弦 / 精排回答"这段内容**回答了**这个问题吗"（分数、可排序）；
       本列回答"这份文档**今天有没有资格被引用**"（布尔、只筛行）。
       **时间永远不进相关性分数** —— 混进去就是"越新越相关"，且 badcase 归因不出来。
       → `AGENTS §7.1` 约定 20、`architecture.md` §9.36、`PLAN §8.6`

    ⚠️ 默认 `ACTIVE`：**阶段 A 零行为变化**（存量文档先都还能被引用），
       再由灌库映射 / 回填把存档版改成 `SUPERSEDED`。
    """

    ACTIVE = "active"  # 现行，可引用
    SUPERSEDED = "superseded"  # 已被新版取代（旧政策 / 已废止）
    DRAFT = "draft"  # 草案，未生效
    SIBLING_BRAND = "sibling_brand"  # 兄弟品牌（森屿 Lab），只在问 Lab 时有关
    INTERNAL = "internal"  # 内部培训 / 口径材料，不该直接引给客户


class DocumentStatus(enum.StrEnum):
    """文档入库状态机：`PENDING → PROCESSING → READY | FAILED`。

    ⚠️ `FAILED` 是**显式状态**，不是"没变成 READY"。
        入库失败必须能在列表里看见、能看到原因 —— 否则用户上传后界面永远转圈。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Document(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """一份上传的原始文档。"""

    __tablename__ = "documents"

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # sha256(原始字节)。用途：同一租户内**避免重复入库**（同文件传两次不该变两份）。
    # ⚠️ 用 sha256 而不是 md5 —— md5 已被证明可构造碰撞，不该再出现在新代码里。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        default=DocumentStatus.PENDING,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------- v1.3：内容有效性（与上面的 `status` 正交，见 `ContentStatus`）----------
    # ⚠️ 加这三列**不需要重嵌入**：向量在 `chunks.embedding`，状态在 `documents`，
    #    检索时 join 一下即可 —— 这正是"向量库和业务数据同在一个 PG"的红利。
    content_status: Mapped[ContentStatus] = mapped_column(
        pg_enum(ContentStatus, "document_content_status"),
        default=ContentStatus.ACTIVE,
        # ⚠️ 必须给 server_default：`ALTER TABLE ... ADD COLUMN NOT NULL` 要有默认值，
        #    否则存量行没法补。它同时也是"阶段 A 不改行为"的保证。
        server_default=ContentStatus.ACTIVE.value,
        nullable=False,
        index=True,
    )
    #: 生效日（空 = 一直有效）。政策原文自带的日期；读不到就留空。
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: 失效日（空 = 仍然有效）。通常等于新版的 `effective_from`。
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Chunk.ordinal",
        lazy="raise",
    )

    __table_args__ = (
        # 全局唯一的是 (tenant_id, content_hash) 两列一起。
        # 不同租户上传同一个文件是**正常**的（各有各的知识库），不能互相挡。
        UniqueConstraint("tenant_id", "content_hash"),
        enum_check("status", DocumentStatus, "document_status"),
        enum_check("content_status", ContentStatus, "document_content_status"),
    )

    def __repr__(self) -> str:
        return f"<Document {self.filename} {self.status.value}>"


class Chunk(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """文档切分后的一段，带向量。检索的最小单位。"""

    __tablename__ = "chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # 在文档内的序号。检索命中后要能"往前/往后看一眼"（扩展上下文窗口），得靠它。
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # ⚠️ 可空：入库是 `PENDING → PROCESSING` 时先落文本、再算向量。
    #    把向量列设成 NOT NULL 会逼着"文本+向量一次性写入"，那样解析和向量化就没法分阶段重试了。
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    # 元数据锁的两半 —— 见模块 docstring
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped[Document] = relationship(back_populates="chunks", lazy="raise")

    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)

    def __repr__(self) -> str:
        return f"<Chunk doc={self.document_id} #{self.ordinal}>"
