"""知识库文档相关的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.knowledge import DocumentStatus


class DocumentOut(BaseModel):
    """列表项 / 上传结果。

    ⚠️ **没有 `content_hash`**：它是判重用的内部口径，对界面没有任何用处。
        多暴露一个字段 = 多一个以后不敢改的兼容负担。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str | None
    size_bytes: int | None
    status: DocumentStatus
    #: 失败原因。**只有 FAILED 时才有值** —— 界面据此显示"为什么没进库"。
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentUploadOut(DocumentOut):
    """上传接口的返回：多两件只有"这次请求"才知道的事。"""

    #: 这次请求是不是真的新建了一份文档。
    #: ⚠️ `False` 不代表失败 —— 判重命中了（同一份内容已经在库里）。
    created: bool
    #: 给人看的一句话说明（判重命中时说明"为什么没重复入库"）。
    note: str = ""


class DocumentListOut(BaseModel):
    """列表：`total` 与 `items` 分开。

    ⚠️ 分页的接口**必须**给总数，否则界面只能显示"这一页有 20 条"，
        说不出"一共有多少" —— 用户就没法判断是不是还有下一页。
    """

    total: int
    items: list[DocumentOut]
