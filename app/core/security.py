"""密码哈希与 JWT 签发 / 校验。

⚠️ **为什么是 bcrypt 而不是 argon2**：
    argon2-cffi 依赖 C 扩展 `argon2-cffi-bindings`；Python 3.13 上不保证有预编译 wheel，
    没有就得现场编译（CI 变慢、容器构建可能直接失败）。
    bcrypt 提供 **abi3 wheel**（cp39+ 通用），3.13 直接装。
    **在这个项目里，"装得上、可复现" 优先于 "理论强度更高"。**

⚠️ **bcrypt 的 72 字节上限**：超出部分会被**静默截断** ——
    也就是说"很长的密码"和"它的前 72 字节"完全等价。
    这里选择**直接拒绝**而不是悄悄截断：静默截断会让用户以为长密码更安全，
    是一个真实存在的安全坑（同类问题在项目 #1 里踩过）。

⚠️ **bcrypt 是 CPU 密集的**（这正是它抗暴力破解的原因）。
    在 async 路由里**直接调它会把事件循环卡住** → 必须丢进线程池。
    做法见 `app/api/auth.py` 的 `run_in_threadpool`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings

# bcrypt 的硬性上限（算法本身，不是我们定的）
BCRYPT_MAX_PASSWORD_BYTES = 72

# ⚠️ "bearer" 是 OAuth2 的固定字面量，不是密钥
TOKEN_TYPE = "bearer"  # noqa: S105


class InvalidCredentialsError(Exception):
    """密码错误。

    刻意**不复用** `ValueError`：调用方要能一眼区分"密码错"和"参数写错了"。
    """


class PasswordTooLongError(ValueError):
    """密码超过 bcrypt 的 72 字节上限 —— 拒绝，不截断。"""


class TokenPayload(BaseModel):
    """JWT 载荷。字段尽量短（token 会放进每个请求头）。"""

    model_config = ConfigDict(frozen=True)

    sub: uuid.UUID  # 用户 id
    tid: uuid.UUID  # 租户 id —— **每个请求都要用它做隔离**
    role: str
    exp: datetime


def hash_password(password: str, *, rounds: int | None = None) -> str:
    """把明文密码哈希成可入库的字符串。

    Args:
        password: 明文密码。
        rounds: bcrypt cost；默认取配置 `password_hash_rounds`。

    Raises:
        PasswordTooLongError: 超过 72 字节（UTF-8 编码后）。
    """
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"密码超过 {BCRYPT_MAX_PASSWORD_BYTES} 字节上限"
            f"（当前 {len(raw)} 字节）。bcrypt 会静默截断多余部分，因此这里直接拒绝。"
        )

    cost = get_settings().password_hash_rounds if rounds is None else rounds
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=cost)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码是否匹配。

    ⚠️ 任何异常（哈希串被改坏、编码问题、超长）都**返回 False**，
        绝不让异常冒到路由层 —— 否则会变成"输错密码 → 500"，
        既能被探测又难看。密码错就是密码错。
    """
    try:
        raw = password.encode("utf-8")
        if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def create_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """签发 JWT。

    ⚠️ **`tid`（租户 id）必须进 token**：每个请求都靠它做数据隔离。
        若让客户端"自己报租户"，改一个请求参数就能读别人家的数据。

    Returns:
        `(token, expires_in_seconds)` —— 后者直接给前端算刷新时机用。
    """
    settings = get_settings()
    minutes = settings.access_token_expire_minutes if expires_minutes is None else expires_minutes
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=minutes)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, minutes * 60


def decode_access_token(token: str) -> TokenPayload:
    """解析并校验 JWT。

    Raises:
        jwt.InvalidTokenError: 签名不对 / 过期 / 结构损坏 —— 一律归为"凭据无效"。
            调用方（app/api/deps.py）统一转成 401，**不要**把底层异常暴露给客户端
            （区分"过期"和"签名错"会给攻击者反馈）。
    """
    settings = get_settings()
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return TokenPayload.model_validate(claims)
