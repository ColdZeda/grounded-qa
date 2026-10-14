"""租户隔离仓储层 —— **全项目最重要的一道防线**。

多租户系统最经典的翻车方式是：某个查询忘了写 `WHERE tenant_id = ?`。
靠 code review 防不住（人会忘、新人更会忘），所以这里把 `tenant_id`
**焊进查询构造**：只要你用这个类，就**没有"忘记加条件"这个选项**。

反面教材（**永远不要这样写**）：
```python
conversation = await session.get(Conversation, conversation_id)   # ← 越权漏洞
```
`session.get()` 只按主键查，不带任何租户条件。攻击者遍历 UUID 就能读到别人的数据。

正确写法：
```python
repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Conversation)
conversation = await repo.get(conversation_id)   # 自带 tenant_id 条件，跨租户必然 None
```
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base


class MissingTenantColumnError(TypeError):
    """模型没有 `tenant_id` —— 说明它忘了继承 `TenantMixin`。

    ⚠️ 这里**故意抛异常而不是静默放过**：一个没有 tenant_id 的表出现在仓储层，
        意味着它绕过了隔离机制。宁可启动就炸，也不要带着漏洞跑。
    """


class TenantScopedRepository[ModelT: Base]:
    """把某个模型的所有读写**限定在一个租户内**。"""

    __slots__ = ("_model", "_session", "_tenant_id")

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        model: type[ModelT],
    ) -> None:
        if "tenant_id" not in model.__table__.columns:
            raise MissingTenantColumnError(
                f"{model.__name__} 没有 tenant_id 列 —— 它是不是忘了继承 TenantMixin？"
            )
        self._session = session
        self._tenant_id = tenant_id
        self._model = model

    # ---------- 只读属性 ----------

    @property
    def session(self) -> AsyncSession:
        return self._session

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    @property
    def model(self) -> type[ModelT]:
        return self._model

    # ---------- 查询构造 ----------

    def select(self) -> Select[tuple[ModelT]]:
        """返回**已注入租户条件**的 SELECT。

        需要加更多条件（比如"只要我自己的"）时，在返回值上继续 `.where(...)`，
        **不要**绕开它自己 `select(Model)`。
        """
        return select(self._model).where(self._model.tenant_id == self._tenant_id)  # type: ignore[attr-defined]

    async def get(self, obj_id: uuid.UUID) -> ModelT | None:
        """按主键取一条 —— **同时限定租户**。跨租户必然返回 `None`。"""
        stmt = self.select().where(self._model.id == obj_id)  # type: ignore[attr-defined]
        return await self._session.scalar(stmt)

    async def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[ModelT]:
        """按创建时间倒序列出本租户的记录。"""
        stmt = (
            self.select()
            .order_by(self._model.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()

    async def count(self) -> int:
        """本租户的记录总数（跨租户的不会被数进来）。"""
        subquery = self.select().subquery()
        total = await self._session.scalar(select(func.count()).select_from(subquery))
        return int(total or 0)

    # ---------- 写入 ----------

    def build(self, **kwargs: Any) -> ModelT:
        """构造一个新对象，**`tenant_id` 由仓储注入**。

        ⚠️ 调用方传 `tenant_id` 会直接报错 —— 这是**有意的**：
            写接口如果能"自己选租户"，就等于把隔离交给了输入校验，早晚出事。
        """
        if "tenant_id" in kwargs:
            raise ValueError("tenant_id 由仓储注入，调用方不得传入")
        return self._model(tenant_id=self._tenant_id, **kwargs)

    async def add(self, obj: ModelT) -> ModelT:
        """把对象加进会话（**不 commit** —— 提交时机由路由决定）。"""
        self._session.add(obj)
        await self._session.flush()
        return obj
