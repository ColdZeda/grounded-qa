"""pytest 全局夹具。

⚠️ 测试**刻意分两类**：

| 类别 | 标记 | 需要什么 | 跑法 |
|---|---|---|---|
| 纯单元 | （无） | 什么都不需要 | `uv run pytest -q` |
| 集成 | `@pytest.mark.integration` | **真实 PostgreSQL 且已迁移** | `docker compose up -d` + `uv run alembic upgrade head` |

⚠️ 集成测试**连不上库就 SKIP**（不是 FAIL），跳过信息里写清缺什么 ——
    没起 compose 的人不该看到一片红。但**绝不静默**：`uv run pytest -q -rs` 会列出跳过原因。

⚠️ **为什么每个测试各建一个引擎，而且用 `NullPool`**：
    pytest-asyncio 默认**每个测试函数一个新事件循环**，而 asyncpg 的连接**绑定事件循环**。
    共享一个全局连接池 → 从第二个测试开始就会撞上
    `Task got Future attached to a different loop`（这个报错非常难懂，且看起来像业务 bug）。
    每个测试自建引擎 + 不复用连接 = 引擎与循环同生共死，从根上避开整类问题。
    代价只是每个测试多几次 TCP 握手，本地完全可接受。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

# ⚠️ **测试不该取决于开发机器的 `.env`。**
# 本地为了演示把 `OPEN_AGENT_MODE` 打开了，而百多个 pipeline 用例默认要走 pipeline。
# 环境变量优先级**高于** `.env`，所以这里强制写死；要测 agent 的用例自己 monkeypatch。
os.environ["OPEN_AGENT_MODE"] = "false"

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.deps import get_limits_redis, get_session
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.main import create_app
from tests.fakeredis import FakeRedis

# ⚠️ 测试里 bcrypt 用**最低轮数**：被测的是"逻辑对不对"，不是"暴力破解要多久"。
#    用生产默认的 12 轮会让每个用例慢几百毫秒，整个测试套就没人愿意跑了。
TEST_BCRYPT_ROUNDS = 4

#: 建号用的固定密码。集成测试里反复用，改一处即可。
TEST_PASSWORD = "test-password-123"


@dataclass(frozen=True)
class Account:
    """一个测试账号 + 它所属的租户。"""

    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    email: str
    password: str
    role: UserRole

    def auth(self, token: str) -> dict[str, str]:
        """给请求加 Bearer 头（token 得先用 `login` fixture 换）。"""
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """连得上、且已迁移的数据库引擎；否则 skip。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
            migrated = await conn.scalar(text("select to_regclass('public.users')"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(
            f"集成测试跳过：连不上 PostgreSQL（{type(exc).__name__}）。"
            f"先起本地依赖栈：docker compose up -d"
        )

    if migrated is None:
        await engine.dispose()
        pytest.skip("集成测试跳过：库里还没有业务表。先跑：uv run alembic upgrade head")

    yield engine
    await engine.dispose()


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """给测试直接读写数据库用的会话（**绕开 HTTP 层**）。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        yield s


@pytest.fixture
def app() -> FastAPI:
    """应用实例。

    ⚠️ 单独暴露出来，是为了让测试能往 `app.dependency_overrides` 里塞替身 ——
        典型场景：把模型客户端换成假的，避免真花钱（AGENTS §7.1 约定 9）。
        覆盖表是**请求时**才读的，所以测试在拿到 `client` 之后再设置也生效。
    """
    return create_app()


@pytest.fixture
def limits_redis() -> FakeRedis:
    """限流 / 配额用的**内存** Redis。

    ⚠️ **必须给所有 API 测试都用它**：真 Redis 会让整套测试共享同一个计数器，
        跑几十个聊天用例就把"30 次/分钟"用光，后面的用例开始 429 ——
        单跑都过、连跑就挂，是那种最难查的 flaky。
    """
    return FakeRedis()


@pytest.fixture
async def client(
    app: FastAPI, db_engine: AsyncEngine, limits_redis: FakeRedis
) -> AsyncIterator[AsyncClient]:
    """走 ASGI 的应用客户端。

    ⚠️ **用 `dependency_overrides` 把会话依赖换成测试引擎**，而不是让应用用它的全局引擎 ——
        全局引擎是进程级单例，会跨事件循环复用连接，正是上面说的那类 flaky 来源。

    ⚠️ **限流用的 Redis 也换成内存实现**（见 `limits_redis` fixture）。

    ⚠️ **不跑 lifespan**（`ASGITransport` 默认不触发）：
        lifespan 启动时会做全文检索自检、退出时会 `dispose_engine()` / 关 HTTP 客户端，
        而这里根本没用到那些全局对象。
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session
    # 限流/配额走内存假 Redis（见 `limits_redis` fixture 的说明）
    app.dependency_overrides[get_limits_redis] = lambda: limits_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def make_account(
    session: AsyncSession,
) -> AsyncIterator[Callable[..., Awaitable[Account]]]:
    """建"租户 + 用户"的工厂，**测试结束自动清理**。

    清理只需要删租户：`tenants` 上的外键都是 `ON DELETE CASCADE`，
    用户 / 会话 / 消息 / 文档 / 分块会一起消失 —— 这也顺带验证了级联是配对的。
    """
    created_tenants: list[uuid.UUID] = []

    async def _make(
        *,
        role: UserRole = UserRole.ADMIN,
        label: str = "acct",
        tenant: Tenant | None = None,
    ) -> Account:
        if tenant is None:
            tenant = Tenant(slug=f"{label}-{uuid.uuid4().hex[:10]}", name=f"租户 {label}")
            session.add(tenant)
            await session.flush()
            created_tenants.append(tenant.id)

        email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
        user = User(
            tenant_id=tenant.id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD, rounds=TEST_BCRYPT_ROUNDS),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()

        return Account(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            user_id=user.id,
            email=email,
            password=TEST_PASSWORD,
            role=role,
        )

    yield _make

    for tenant_id in created_tenants:
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await session.commit()


@pytest.fixture
async def login(client: AsyncClient) -> Callable[[str, str], Awaitable[str]]:
    """登录并返回 access_token（失败就直接断言失败，便于定位）。"""
    prefix = get_settings().api_prefix

    async def _login(email: str, password: str) -> str:
        resp = await client.post(
            f"{prefix}/auth/login",
            data={"username": email, "password": password},
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["access_token"])

    return _login
