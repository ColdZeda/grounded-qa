"""鉴权相关的出入参。"""

from __future__ import annotations

import uuid
from typing import Literal

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
    #: 是不是一键演示账号（**只读管理员**：看得到全部页面，写操作一律 403）。
    #: 前端据此隐藏「上传 / 替换 / 删除 / 建成员」这些按钮，别让人点了才吃 403。
    is_demo: bool = False
    #: 界面偏好（目前只有 `chat_mode`）。前端用它回显上次选的模式。
    preferences: dict[str, object] | None = None
    #: ：agent 模式可不可用（= 系统总闸 `open_agent_mode`）。
    #: ⚠️ 前端据此把模式开关**置灰**；不给这个信号的话，
    #:    用户会以为自己切到了 agent，而实际走的是 pipeline。
    agent_available: bool = False


class PasswordChange(BaseModel):
    """改自己的密码（走查阶段 P0-②）。

    ⚠️ 长度校验的取舍与 `LoginRequest` 一致：`max_length` 用**字符数**兜底，
       真正的上限是 **bcrypt 的 72 字节** —— 由 `hash_password` 明确拒绝，
       绝不静默截断（截断等于"后半段密码不参与校验"）。
    """

    old_password: str = Field(min_length=1, max_length=64, description="当前密码")
    new_password: str = Field(min_length=8, max_length=64, description="新密码（≥8 位）")


class Capabilities(BaseModel):
    """前端可用的登录方式（`GET /auth/capabilities`，无需鉴权）。

    ⚠️ **不含任何密钥类信息**，只有布尔开关 —— 前端据此决定登录页要不要显示
        「一键体验」。真正的演示账号在服务端 find-or-create，**密码压根不存在**。
    """

    demo_login: bool


class PreferencesIn(BaseModel):
    """更新界面偏好。目前只有一项：下次提问默认用哪条路。

    ⚠️ 只收白名单字段：`preferences` 是 JSONB，不限制的话会变成“什么都能往里塞”的黑洞，
        而界面偏好里一旦混进权限 / 计费字段，就是一个提权漏洞。
    """

    chat_mode: Literal["fast", "agent"] | None = None
