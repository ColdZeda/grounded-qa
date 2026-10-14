"""建库引导：**默认租户 + 管理员账号**（幂等）。

用法：

    uv run python -m app.db.seed

⚠️ **幂等** —— 按 `tenants.slug` / `users.email` 查，有就复用，重复跑不会造出两份。
    所以可以放心放进启动脚本（本地起栈后跑一次）。

⚠️ **生产不许用默认密码**：`admin_password` 还是 `change-me` 而 `APP_ENV != dev`
    时直接**报错退出**，而不是"建一个弱密码管理员然后祈祷没人发现"。
    公网机器上，一个 admin/change-me 就是全站数据。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import DEFAULT_ADMIN_PASSWORD, get_settings
from app.core.security import hash_password
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.session import dispose_engine, get_session_factory


async def seed() -> None:
    """建默认租户 + 管理员（幂等）。"""
    settings = get_settings()

    if settings.app_env != "dev" and settings.admin_password == DEFAULT_ADMIN_PASSWORD:
        raise SystemExit(
            "拒绝执行：APP_ENV 不是 dev，但 ADMIN_PASSWORD 仍是默认值。\n"
            "请在 .env 里设一个真实密码后再跑 seed。"
        )

    email = settings.admin_email.strip().lower()
    factory = get_session_factory()

    async with factory() as session:
        # ---------- 租户 ----------
        tenant = await session.scalar(
            select(Tenant).where(Tenant.slug == settings.default_tenant_id)
        )
        if tenant is None:
            tenant = Tenant(slug=settings.default_tenant_id, name="默认租户")
            session.add(tenant)
            await session.flush()  # 拿到 tenant.id
            print(f"[seed] 新建租户 slug={tenant.slug} id={tenant.id}")
        else:
            print(f"[seed] 租户已存在 slug={tenant.slug} id={tenant.id}（未改动）")

        # ---------- 管理员 ----------
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                tenant_id=tenant.id,
                email=email,
                hashed_password=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(user)
            print(f"[seed] 新建管理员 {email}（租户 {tenant.slug}）")
        else:
            # ⚠️ 已存在时**不改密码** —— 改密码是运营动作，不该被一个"引导脚本"悄悄做掉。
            #    但要把租户 / 角色对齐，否则会出现"admin 挂在错误租户下"这种诡异状态。
            changed = False
            if user.tenant_id != tenant.id:
                user.tenant_id = tenant.id
                changed = True
            if user.role is not UserRole.ADMIN:
                user.role = UserRole.ADMIN
                changed = True
            print(
                f"[seed] 管理员已存在 {email}"
                f"{'（已修正租户/角色）' if changed else '（未改动，密码保持原样）'}"
            )

        await session.commit()

    print("[seed] 完成。")


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    main()
