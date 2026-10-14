"""API 出入参模型（Pydantic）。

⚠️ 这些是**对外契约**，和 ORM 模型分开：
    直接把 ORM 对象当响应体会把 `hashed_password` 这类字段泄露出去。
    分开写虽然多几个类，但"哪些字段能被外部看到"变成一件**显式**的事。
"""

from app.schemas.auth import LoginRequest, TokenOut, UserOut
from app.schemas.conversation import ConversationCreate, ConversationOut
from app.schemas.tenant import TenantOut
from app.schemas.user import UserSummary

__all__ = [
    "ConversationCreate",
    "ConversationOut",
    "LoginRequest",
    "TenantOut",
    "TokenOut",
    "UserOut",
    "UserSummary",
]
