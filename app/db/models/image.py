"""用户提问时上传的图片（**query 侧**，绝不进知识库）。

三条边界，写清楚比实现更重要：

⚠️ **① 图不属于知识库。** 它不进 `chunks`、不进向量空间、不进检索候选。
   知识库是**租户共有**的；图是**某个用户某次提问的附件**，生命周期跟着消息走。
   混在一起的话，A 用户上传的图会出现在 B 用户的检索结果里 —— 那是越权，不是 bug。

⚠️ **② 文件在卷上，这一行只是索引。** `path` 相对 `upload_dir`；
   换存储（对象存储）时只改这一层，不改表结构。
   ⚠️ 命名卷的属主来自镜像 —— 目录必须 `mkdir -p` + `chown`，
      漏了就是「本地正常、容器 500」（`AGENTS.md` §10 有这条坑）。

⚠️ **③ 分析结果（四字段）**存在 `analysis` 里，**同一张图只分析一次**：
   它是"读图的账本"，也是 badcase 复盘的唯一证据（"当时模型到底读到了什么"）。
   ⚠️ 但**多轮追问不再看它** —— 追问靠的是 `messages.meta` 里那段文字记忆
      （见 `architecture.md` §9.35 的多轮方案），所以历史渲染不需要 JOIN 这张表。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Image(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """一张用户上传的图。"""

    __tablename__ = "images"

    #: 上传者。取图 / 渲染缩略图都要按它过滤（越权一律 404）。
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 内容哈希。用途：① 完整性校验 ② 将来若要做"同图复用分析结果"，键就是它。
    #: ⚠️ 现在**不做**这个缓存（理由见 `architecture.md` §9.35），但哈希本来就该算。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: 相对 `upload_dir` 的路径，如 `images/<tenant>/<image_id>/<sha256>.png`
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 读图结果（四字段 ＋ 元信息）：
    #: `{model, image_type, scene, transcription, has_exact_fields, fallback}`
    #: ⚠️ `fallback=true` 表示"模型没按 JSON 回，我们把整段当 scene 了" ——
    #:    这个标记必须留下来，否则降级是**静默**的（本项目最忌讳的那种坏）。
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: 读图失败的原因（模型不可用 / 超时）。**不许把失败当空结果**。
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Image {self.id} {self.content_type} {self.bytes}B>"
