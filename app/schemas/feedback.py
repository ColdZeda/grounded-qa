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
    #: **客户问的那一句**（2026-10-28 D3）。迁移前的老记录是 `None` ——
    #: 界面要明说"这条收集时还没存提问"，不能当成"客户什么都没问"。
    question_snapshot: str | None = None
    #: 这条回答属于哪个会话（「打开原会话」用）。会话被删后为 `None`。
    conversation_id: uuid.UUID | None = None
    # ---- 「采纳入库」流程（D11）。四个时间/人字段就是**操作留痕** ----
    adopted_document_id: uuid.UUID | None = None
    adopted_by: uuid.UUID | None = None
    adopted_at: datetime | None = None
    published_by: uuid.UUID | None = None
    published_at: datetime | None = None


class FeedbackAdminOut(FeedbackOut):
    """管理员清单用：多给"谁做的"和"草稿现在什么状态"。

    ⚠️ 邮箱在**服务端**翻译成字符串，不让前端拿 UUID 去猜 ——
        治理清单上写 `采纳人：a3f8…` 等于没写。
    """

    adopted_by_email: str | None = None
    published_by_email: str | None = None
    #: 草稿文档的 `content_status`（`draft` = 还没发布，`active` = 已发布）。
    adopted_document_status: str | None = None
    #: 草稿文件名（`feedback/<id>.md`），便于到知识库页对照。
    adopted_document_name: str | None = None


class AdoptIn(BaseModel):
    """采纳一条反馈 → 生成草稿。

    ⚠️ **两个字段都可选**：不填就按快照原样生成草稿。
        填了 `content` 就是"人工改写后的正确说法" —— 这正是采纳的意义，
        把坐席的判断写进知识库，而不是把错答案原样搬进去。
    """

    title: str | None = Field(default=None, max_length=120)
    content: str | None = Field(default=None, max_length=8000)


class AdoptOut(BaseModel):
    """采纳结果。`chunks` = 0 说明只落了文本没嵌入（缺 embedding key）。"""

    document_id: uuid.UUID
    document_name: str
    content_status: str
    chunks: int
    note: str | None = None


class DraftOut(BaseModel):
    """草稿全文（审阅用）。**草稿不参与检索**，只能从这里看。"""

    document_id: uuid.UUID
    document_name: str
    content_status: str
    title: str
    content: str
    adopted_at: datetime | None
    published_at: datetime | None
    #: 草稿里能被冲突检测认出来的期限事实（发布前的体检是另一回事，见 publish）。
    chunks: int


class PublishIn(BaseModel):
    """发布草稿。`force=True` 表示"体检报了冲突，我看过了，仍然要发"。"""

    force: bool = False


class PublishOut(BaseModel):
    """发布结果。`conflicts` 是**这次体检**发现的、和本草稿有关的那几条。"""

    document_id: uuid.UUID
    document_name: str
    content_status: str
    published_at: datetime
    forced: bool
    conflicts: list[dict[str, object]]
