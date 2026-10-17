"""FastAPI 依赖：数据库会话、当前用户、权限校验。

⚠️ **这里是鉴权的唯一入口。**
    路由**绝不能**自己解析 token、自己查用户表 —— 一旦存在第二套写法，
    就一定会有一套忘了校验 `is_active`、或者忘了取 `tenant_id`。
    要加"某种新的登录方式"（比如 API Key），改这里，不要改路由。
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.session import get_session
from app.workers.queue import get_redis

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# ⚠️ `auto_error=False`：缺 token 时不要 FastAPI 的英文报错，
#    我们要自己给**统一**的 401 + `WWW-Authenticate` 头（OAuth2 规范要求）。
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Bearer <登录接口返回的 access_token>",
)


def _unauthorized(detail: str = "凭据无效或已过期") -> HTTPException:
    """统一的 401。

    ⚠️ 无论"没带 token / 签名错 / 过期 / 用户被停用"，**对外都是同一句话**。
        区分开来等于告诉攻击者"你的 token 是真的，只是过期了"或者"这个用户存在"。
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """解析 Bearer token → 返回**数据库里的**用户对象。

    ⚠️ 拿到 token 后**必须回库查一次**，不能只信 token 里的内容：
        用户可能已被停用、租户可能已被停用。JWT 是自包含的，改不了 ——
        这正是"无状态"的代价，**必须靠回库校验补上**。

    ⚠️ 还要核对 `token.tid == user.tenant_id`：
        万一某天要做"用户换租户"，旧 token 里还带着**旧租户 id**，
        不核对的话就会出现"人已经不在这个租户了，token 还能读它的数据"。
        这个字段平时是冗余的，**冗余的那一份用于发现不一致**。
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized("缺少 Bearer token")

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        # 不区分"过期"和"签名错"，统一 401
        raise _unauthorized() from exc

    stmt = (
        select(User)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(
            User.id == payload.sub,
            User.is_active.is_(True),
            # 租户被停用 → 它下面所有用户立即失效（运营侧的一键止血手段）
            Tenant.is_active.is_(True),
        )
    )
    user = await session.scalar(stmt)
    if user is None:
        raise _unauthorized()

    if user.tenant_id != payload.tid or user.role.value != payload.role:
        # 用户被改过租户或角色 → 让客户端重新登录，而不是继续用旧权限
        raise _unauthorized("凭据已失效，请重新登录")

    return user


async def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """要求管理员角色。

    ⚠️ **管理员是"租户内的管理员"**，不是超级管理员 ——
        他只能管自己租户的东西。跨租户的运营操作**没有 API**，只能手工做。
        这条边界很重要：多租户系统里最危险的 bug 就是"某个 admin 接口忘了限定租户"。
    """
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user


# 路由里直接用这两个别名，写法统一、少一层括号
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


# ------------------------------------------------------------
# 限流 / 配额用的 Redis
# ------------------------------------------------------------
def get_limits_redis() -> Redis:
    """限流与 token 配额用的连接。

    ⚠️ 和入库队列**共用同一个客户端单例**：同一个进程没必要开两个连接池。
        两者键前缀不同（限流是 `gq:rl:` / `gq:quota:`，队列是 `gq:jobs:`），互不干扰。

    ⚠️ 做成依赖是为了**测试能换掉**：测试里塞一个内存假 Redis，
        就能跑**真逻辑**（窗口对齐、fail-open 全覆盖），而不是塞个假 guard 把逻辑整段跳过。
    """
    return get_redis()


LimitsRedisDep = Annotated[Redis, Depends(get_limits_redis)]
