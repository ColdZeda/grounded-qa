"""鉴权相关的出入参。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import TOKEN_TYPE
from app.db.models.user import UserRole


class LoginRequest(BaseModel):
    """JSON 登录请求（前端用这个）。

    ⚠️ `password` 的 `max_length` 用**字符数**兜底，真正的上限是**字节数**（bcrypt 72 字节）。
        一个中文字符占 3 字节 —— 所以这里取 72 // 3 = 24 是**过于保守**了，
        会让正常长度的中文密码被拒。这里放宽到 64 字符：
        纯 ASCII 时 64 < 72 安全；中文时 64 字符 = 192 字节，会**在 security 层被明确拒绝**
        （而不是静默截断）。两道防线，报错信息清楚。
    """

    email: EmailStr
    password: str = Field(min_length=1, max_length=64)


class TokenOut(BaseModel):
    """登录成功的返回。"""

    access_token: str
    # ⚠️ 用常量而不是字面量 "bearer"：这是 OAuth2 规范固定的**类型标签**，不是密钥。
    #    写成字面量会让安全扫描（ruff 的 S105）误报"硬编码密码"。
    token_type: str = TOKEN_TYPE
    # 秒。前端拿它决定什么时候该重新登录，不用自己解析 JWT。
    expires_in: int


class UserOut(BaseModel):
    """当前用户信息。**刻意不含 `hashed_password`。**"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    tenant_id: uuid.UUID
