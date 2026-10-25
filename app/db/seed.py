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
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEFAULT_ADMIN_PASSWORD, get_settings
from app.core.security import hash_password
from app.db.models.order import Order, OrderStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.session import dispose_engine, get_session_factory

#: 20 条演示订单。**刻意覆盖客服真正会遇到的分支**：
#:
#:   · 签收 1~7 天   → 质量问题 30 天内，可退换
#:   · 签收 35 / 45 天 → **超过 30 天 → 该转保修，不是退款**（这是最容易被答错的一类）
#:   · 已取消 / 未发货 / 在途 / 退款中 —— 每种状态都该有样本，否则模型只见过"已签收"
#:
#: ⚠️ 日期按「相对今天」算，**不写死绝对值**：写死的话几个月后"签收 45 天"会漂成 200 天，
#:    而评测问的是"有没有超过 30 天"这种相对判断。
#: ⚠️ 单号要与 `evals/fixtures/images.meta.yaml` 里的图对齐（P1=SF10001 / P2=SF10002）。
_ORDER_SPECS: tuple[tuple[str, OrderStatus, int, int | None, int, list[dict[str, object]]], ...] = (
    ("SF10001", OrderStatus.DELIVERED, 6, 4, 128_000, [{"name": "实木床架", "qty": 1}]),
    ("SF10002", OrderStatus.DELIVERED, 8, 6, 45_900, [{"name": "床头柜", "qty": 1}]),
    ("SF10003", OrderStatus.PAID, 2, None, 89_900, [{"name": "书桌", "qty": 1}]),
    ("SF10004", OrderStatus.SHIPPED, 7, None, 328_000, [{"name": "三人沙发", "qty": 1}]),
    ("SF10005", OrderStatus.REFUNDING, 20, 18, 159_900, [{"name": "餐边柜", "qty": 1}]),
    ("SF10006", OrderStatus.CANCELLED, 30, None, 25_900, [{"name": "落地灯", "qty": 1}]),
    # ⚠️ 签收 45 天：超过质量问题 30 天窗口 → 正确回答是"转保修"，不是"能退"
    ("SF10007", OrderStatus.DELIVERED, 60, 45, 128_000, [{"name": "实木床架", "qty": 1}]),
    ("SF10008", OrderStatus.DELIVERED, 3, 1, 56_800, [{"name": "餐椅", "qty": 2}]),
    ("SF10009", OrderStatus.DELIVERED, 90, 80, 9_900, [{"name": "小边几", "qty": 1}]),
    ("SF10010", OrderStatus.SHIPPED, 1, None, 259_900, [{"name": "衣柜", "qty": 1}]),
    ("SF10011", OrderStatus.PAID, 10, None, 15_900, [{"name": "收纳箱", "qty": 3}]),
    ("SF10012", OrderStatus.DELIVERED, 15, 12, 88_000, [{"name": "书架", "qty": 1}]),
    ("SF10013", OrderStatus.CANCELLED, 45, None, 45_900, [{"name": "床头柜", "qty": 1}]),
    ("SF10014", OrderStatus.REFUNDING, 5, 3, 39_900, [{"name": "玄关柜", "qty": 1}]),
    # ⚠️ 签收 35 天：又一个"过窗口"的样本，金额大，适合演示"先维修后换新"
    ("SF10015", OrderStatus.DELIVERED, 40, 35, 1_280_000, [{"name": "全屋家具套系", "qty": 1}]),
    ("SF10016", OrderStatus.SHIPPED, 4, None, 76_800, [{"name": "阳台桌椅", "qty": 1}]),
    ("SF10017", OrderStatus.PAID, 1, None, 4_500, [{"name": "靠垫", "qty": 4}]),
    ("SF10018", OrderStatus.DELIVERED, 120, 110, 19_900, [{"name": "地毯", "qty": 1}]),
    ("SF10019", OrderStatus.REFUNDING, 8, 7, 66_000, [{"name": "电视柜", "qty": 1}]),
    ("SF10020", OrderStatus.DELIVERED, 2, 1, 39_900, [{"name": "茶几", "qty": 1}]),
)


async def _seed_orders(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """造演示订单（**幂等**：单号已存在就跳过，**不改已有行**）。

    ⚠️ 为什么是"跳过"而不是"按今天重算一遍"：`delivered_at` 是**签收这个事实**，
        每次部署都把它往前推一次的话，"签收多少天"就不再是稳定量 ——
        评测和演示会跟着部署时间漂。
    """
    existing = set(
        (await session.scalars(select(Order.order_no).where(Order.tenant_id == tenant_id))).all()
    )
    now = datetime.now(UTC)
    created = 0
    for order_no, status, placed_days, delivered_days, amount, items in _ORDER_SPECS:
        if order_no in existing:
            continue
        session.add(
            Order(
                tenant_id=tenant_id,
                user_id=user_id,
                order_no=order_no,
                status=status,
                placed_at=now - timedelta(days=placed_days),
                delivered_at=(
                    None if delivered_days is None else now - timedelta(days=delivered_days)
                ),
                items=items,
                amount_cents=amount,
                currency="CNY",
            )
        )
        created += 1
    print(f"[seed] 订单：新建 {created} 条，已存在跳过 {len(_ORDER_SPECS) - created} 条")


def _guard_admin_password(password: str) -> None:
    """建库前必须有一个**真实的**管理员密码 —— 空值和默认值一律拒绝。

    ⚠️ **为什么闸在这里，而不在 `Settings` 的启动校验里**（2026-10-25 挪过来的）：
        `ADMIN_PASSWORD` 只在"库里还没有这个邮箱"时被读一次，**跑流量时一次都不读**。
        以前它挂在启动校验上，后果实测过：`.env` 里留空 → api crash-loop →
        `depends_on: condition: service_healthy` 把 nginx 一起拖住 → **整站连接被拒**，
        而且**回滚救不了**（回滚换镜像，`.env` 不在版本控制里）。
        ⟹ 闸门放在**真正用它的那一刻**：站点照常启动（只打告警），建库这一步响亮地失败。

    ⚠️ **空值必须单独判**：`.env` 写成 `ADMIN_PASSWORD=` 读出来是**空字符串**，
        它不等于 `change-me`；只比较"是不是默认值"会把它放过去，
        然后这里就会建一个**空密码的管理员**（等于没有密码）。
    """
    if not password.strip():
        raise SystemExit(
            "拒绝执行：ADMIN_PASSWORD 是空的。\n"
            ".env 里 `ADMIN_PASSWORD=` 这种写法读出来是空字符串 —— 用空密码建管理员等于没有密码。\n"
            "请在 .env 里设一个真实密码后再跑 seed。"
        )
    if password == DEFAULT_ADMIN_PASSWORD:
        raise SystemExit(
            "拒绝执行：APP_ENV 不是 dev，但 ADMIN_PASSWORD 仍是默认值。\n"
            "请在 .env 里设一个真实密码后再跑 seed。"
        )


async def seed() -> None:
    """建默认租户 + 管理员（幂等）。"""
    settings = get_settings()

    if settings.app_env != "dev":
        _guard_admin_password(settings.admin_password)

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

        # ---------- 演示订单（20 条，只读业务表）----------
        await _seed_orders(session, tenant.id, user.id)

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
