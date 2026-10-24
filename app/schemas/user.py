"""用户相关的出入参。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

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


class UserCreate(BaseModel):
    """管理员新建成员（走查阶段 P0-①）。

    ⚠️ **不叫 `Register`**：这个系统**没有开放注册**（内部 B 端，账号由管理员发）。
       名字跟着语义走 —— 将来真做 SaaS 自助注册，那会是另一个模型（要邮箱验证等）。

    ⚠️ 密码只校验"下限 8 位"，上限交给 `hash_password`：bcrypt 的上限是
       **72 字节**（中文一个字 3 字节），按字节算而不是按字符算，所以
       在这里用 `max_length` 反而会算错 —— 让 `PasswordTooLongError` 兜底。
    """

    email: str = Field(min_length=3, max_length=255, description="登录邮箱，全局唯一")
    password: str = Field(min_length=8, description="明文密码（≥8 位；服务端只存哈希）")
    role: UserRole = Field(default=UserRole.MEMBER, description="member（默认）/ admin")
