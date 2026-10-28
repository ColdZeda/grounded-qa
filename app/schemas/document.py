"""知识库文档相关的出入参。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.knowledge import ContentStatus, DocumentStatus


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
    #: **内容状态**：`active` 现行 / `superseded` 废止 / `draft` 草稿 / `sibling_brand` 同品牌他站 / `internal` 内部稿。
    #: ⚠️ 2026-10-28 加：它和 `status`（入库状态机：排队/入库中/已入库/失败）**正交** ——
    #:    「已入库」只说"切片和向量都好了"，**没说它能不能被检索引用**。
    #:    走查现场就卡在这：草稿明明还没发布，知识库页却只写「已入库」，看不出它是草稿。
    content_status: ContentStatus = ContentStatus.ACTIVE
    #: 失败原因。**只有 FAILED 时才有值** —— 界面据此显示"为什么没进库"。
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    #: 这份文档被切成几块（2026-10-25 走查：列表原来只给文件名/大小/状态，
    #: 看不出切分结果 —— 而切分质量是检索质量的第一道门）。
    #: ⚠️ 字段名**不能叫 `chunks`**：`Document` 上正好有个同名关系（`lazy="raise"`），
    #:    `model_validate(..., from_attributes=True)` 会去读它并抛"未加载"。
    #: ⚠️ 上传接口也用这个 schema，那时是 0（还没切）；列表接口每次都带真数。
    chunk_count: int = 0


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


class ChunkOut(BaseModel):
    """切片的一块（**看上下文**用）。"""

    #: 在这份文档里的序号（0 开始）。检索命中的「第 3 / 12 块」就是它。
    ordinal: int
    content: str
    #: 是不是**用户点开的那一块**（前后相邻的那几块是 `False`）
    hit: bool = False


class DocumentChunksOut(BaseModel):
    """`GET /documents/{id}/chunks?around=` 的返回：**只给相邻的几块**。

    ⚠️ **故意不返回整篇**：一是流量，二是"你看到的是片段"这件事本身要**一直成立**
       （整篇返回会让人以为这就是全文，然后拿别处的原文来对不上）。
    """

    document_id: uuid.UUID
    filename: str
    #: 请求的那一块（越界已被夹到 [0, total-1]）
    ordinal: int
    #: 这份文档一共几块
    total: int
    #: 实际前后各取了几块（入参回显，前端据此说"上下各 N 块"）
    window: int
    #: 升序，包含命中块
    items: list[ChunkOut]
