"""用户相关的出参。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.db.models.user import UserRole


class UserSummary(BaseModel):
    """管理端用户列表项。

    ⚠️ **刻意没有 `hashed_password`。** 用一个专门的"摘要"模型而不是直接返回
        ORM 对象，就是为了让"哪些字段会出门"变成一件**看得见**的事 ——
        否则将来有人给 `User` 加一个敏感字段，它会**自动**出现在接口响应里。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    is_active: bool
