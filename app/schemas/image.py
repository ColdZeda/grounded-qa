"""图片接口的出入参。

⚠️ **不回传 `path` / `sha256`**：前者是实现细节（换存储就变），后者是内部判重口径 ——
    多暴露一个字段，就多一个以后不敢改的兼容负担（和 `DocumentOut` 同一条理由）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ImageOut(BaseModel):
    """上传结果 / 图片元信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_type: str
    bytes: int
    width: int
    height: int
    created_at: datetime
