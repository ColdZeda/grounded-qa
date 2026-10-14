"""异步数据库引擎与会话工厂。

⚠️ **引擎是懒创建 + 进程内单例**，不在模块导入时就建。
    原因：`import app.db.session` 会发生在**任何**用到数据库的地方 ——
    包括只跑纯单元测试的 pytest、以及 Alembic 的 env.py。
    导入即建连接池 = 这些场景全都莫名其妙地要求数据库在线。

⚠️ **连接池按 2C2G 调小**（硬约束 §2）：
    默认 `pool_size=5` 对单进程 uvicorn 够用；`max_overflow` 给一点点弹性即可。
    池子开太大不会让应用更快，只会在数据库那头堆出更多空闲连接吃内存。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """取（必要时创建）全局异步引擎。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            # 连接被中间设备悄悄掐断时，池里的死连接会导致"第一次请求必失败"。
            # pre_ping 会在取用前先探一下，代价极小。
            pool_pre_ping=True,
            # 空闲连接最多留 30 分钟。PG 的 wait_timeout / 云上负载均衡都可能更早断，
            # 到点主动换新连接比等着报错好。
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """取（必要时创建）全局会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            # ⚠️ `expire_on_commit=False`：commit 之后对象属性仍可读。
            #    默认 True 会在 commit 后把所有属性标记为过期，下次访问触发**新的查询** ——
            #    在 async 里这会变成 MissingGreenlet，而且是在"明明刚提交完"的地方炸，很费解。
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。

    ⚠️ **不做自动 commit**。路由里要显式 `await session.commit()` ——
        隐式提交会让"读接口不小心写库"这种事变得悄无声息。
        异常时统一 rollback，保证下一个请求拿到的会话是干净的。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """关闭连接池（应用退出、测试收尾时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
