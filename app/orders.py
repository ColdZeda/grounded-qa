"""订单通道：**从文字里抽单号 → 精确查库 → 变成"事实"交给生成层。**

它是本项目里**第一条"租户内按用户"**的读取路径，也是"第二通道"的样板：

    拒答判定：只看【知识库证据】（`pipeline._should_refuse`）—— 这里的东西不参与
    订单通道：查到就塞进上下文；查不到就**一个字都不塞**

⚠️ **为什么"查不到就一个字都不塞"是这么重要的一条**：
    如果给模型一句"（没查到订单）"，它会顺着这句话往下猜；
    什么都不给，它就没有材料可编 —— **"不许编订单"在代码层就成立了**，不靠提示词自觉。

⚠️ **单号不进检索 query**（`PLAN §8.3` D5 的"三不"）：
    `SF10001` 这种串对 `pg_trgm` 和向量都没有语义，塞进去只会污染召回。
    它的去处只有一处 —— **精确匹配这张表**。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.order import Order, OrderStatus
from app.db.repository import TenantScopedRepository

#: 单号的两种常见形态：
#:   ① 字母前缀 + 数字：`SF10001`、`AB123456`
#:   ② 纯数字长串：`202610210001`
#:
#: ⚠️ **不要写成"≥6 位连续数字"**（最初的方案就是那样）：`SF10001` 只有 5 位数字，
#:    长度门槛一卡，真正该识别的单号反而漏掉。**先按形态捞，再拿候选去库里对**
#:    —— 对不上就当没有，这比在正则里猜格式稳得多。
_ORDER_NO_PATTERN = re.compile(r"\b(?:[A-Za-z]{2,4}\d{4,}|\d{6,})\b")

#: 一次最多看几个候选。多到离谱的串（比如一坨 base64）不该变成十几次 SQL。
MAX_CANDIDATES = 5


def extract_order_candidates(text: str) -> list[str]:
    """从任意文字里捞出**可能是单号**的串（去重、转大写、保序）。

    ⚠️ **LEGACY（2026-10-26 起）：只服务 `pipeline`（含 agent 的回落路径）。**
        agent 路径不猜单号 —— 它用 `get_order` 工具直接查（模型自己决定查哪张）。
        ⚠️ 为什么不删：回落路径还在用。退役条件见 `CANDIDATES.md` W16。

    ⚠️ 这里**只做形态筛选，不做校验** —— 是不是真的单号由数据库回答。
        多捞几个不花钱（一次 `IN` 查询），漏捞才会让"我的单呢"答不上来。
    """
    seen: list[str] = []
    for match in _ORDER_NO_PATTERN.finditer(text or ""):
        candidate = match.group(0).upper()
        if candidate not in seen:
            seen.append(candidate)
        if len(seen) >= MAX_CANDIDATES:
            break
    return seen


async def find_order(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    order_no: str,
) -> Order | None:
    """按单号查**本租户内的**订单。查不到 / 不在本租户 → `None`。

    ⚠️ **2026-10-24 决策 D5 改了这里：从"租户 ＋ 用户"放宽到"只按租户"。**
        原因：Web 端被定为**内部坐席工作台**（顾客侧走飞书）——
        坐席接电话时手里只有顾客报的单号，让他"只查得到自己的单"等于这个功能没有。
        📌 所以现在的隔离线是 **租户**：同租户的同事能查同一张单（内部工具，可接受），
        **跨租户仍然查不到**（`TenantScopedRepository` 把 `WHERE tenant_id = ?` 焊死）。
        → `..\\plan\\PROJECT-PLAN.md` §12.1；改动影响见 `tests/test_orders.py`。
    """
    repo = TenantScopedRepository(session, tenant_id=tenant_id, model=Order)
    stmt = repo.select().where(Order.order_no == order_no.upper())
    return await session.scalar(stmt)


async def resolve_orders(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    text: str,
) -> list[Order]:
    """从一段文字里捞出能对上的订单（可能 0 条、可能多条）。

    用途：① 带图提问时从 `transcription` 里捞 ② 追问时从用户原话里捞。
    两种都便宜 —— **一次数据库查询，不花钱**（贵的是模型调用，不是它）。

    ⚠️ 过滤口径与 `find_order` 一致（**只按租户**，见上面那段说明）。
    """
    candidates = extract_order_candidates(text)
    if not candidates:
        return []
    repo = TenantScopedRepository(session, tenant_id=tenant_id, model=Order)
    stmt = repo.select().where(Order.order_no.in_(candidates))
    rows = (await session.scalars(stmt)).all()
    # 保持候选顺序，别让数据库的返回顺序影响提示词（那会让同样的输入产生不同的 prompt）。
    by_no = {row.order_no: row for row in rows}
    return [by_no[no] for no in candidates if no in by_no]


#: 货币符号。**不追求覆盖全世界**：写错了符号比没有符号更糟（会把人民币显示成美元）。
_CURRENCY_SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "SGD": "S$"}


def format_amount(amount_cents: int, currency: str) -> str:
    """分 → `¥1,280.00`。**除法只在这里做一次**，别让调用方各自算。"""
    symbol = _CURRENCY_SYMBOL.get(currency.upper(), "")
    return f"{symbol}{amount_cents / 100:,.2f}"


def days_since_delivery(order: Order, *, now: Any) -> int | None:
    """签收距今多少天（纯算术，**不做任何判断**）。

    ⚠️ 为什么把天数算好再给模型：让模型自己减日期很容易算错一天，
        而"30 天"是有政策边界的硬阈值 —— 差一天结论就反了。
        ⚠️ 但也**只给天数**：该不该退是知识库条款的事，不在这里下结论。

    ⚠️ **必须按自然日算（`date - date`），不能按 24 小时整日**。
        2026-10-27 真机实测：`SF10001` 在 `10-17 17:05Z` 签收、`10-26 16:03Z` 提问，
        `(now - delivered_at).days` 给 **8**，而自然日是 **9**。
        后果不是"少一天"这么轻 —— 政策写的是"7 **个自然日**内"，
        在第 7 天晚上问"还来得及吗"，24 小时算法会把**还能退**答成**不能退**，
        **结论直接反了**。日历上过了几天就是几天，跟时分秒无关。
    """
    if order.delivered_at is None:
        return None
    delivered = order.delivered_at
    # ⚠️ 两个时刻**可能不在同一个时区**：`delivered_at` 从库里出来是 UTC，
    #    而调用方的 `now` 可能是本地时区（`tools.py` 用的是 `datetime.now().astimezone()`）。
    #    直接各自 `.date()` 相减会在跨日的那几小时里差一天 —— 又是"差一天结论就反"。
    #    所以先统一到 `now` 的时区，再比日历。
    #    📌 **日历基准 = `now` 的时区** = `Asia/Shanghai`（容器的 `TZ` 就是它，见 `Dockerfile`）
    #       ⟹ 自然日按**北京日历**，过零点才加一天。
    #       ⚠️ 2026-10-28 实测：SF10001 签收 `10-17 17:05Z`（北京 10-18 01:05）、
    #          10-27 23:46（北京）提问 → **9 天**。此前这里写"生产容器是 UTC"是错的，
    #          那条错也漏进了 `PLAN §14` 与走查 A15/A16（均已改）。
    if now.tzinfo is not None and delivered.tzinfo is not None:
        delivered = delivered.astimezone(now.tzinfo)
    delta = now.date() - delivered.date()
    return max(delta.days, 0)


def order_snapshot(order: Order, *, now: Any) -> dict[str, Any]:
    """把一条订单压成"事实卡片"，交给提示词和 `messages.meta`。

    只放**可以被核对的事实**：单号 / 状态 / 下单与签收时间 / 距今几天 / 商品 / 金额。
    ⚠️ 不放任何"所以可以退"之类的推论 —— 一旦放进去，模型就会把它当规则复述。
    """
    return {
        "order_no": order.order_no,
        "status": order.status.value,
        "placed_at": order.placed_at.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "days_since_delivery": days_since_delivery(order, now=now),
        "items": list(order.items or []),
        "amount": format_amount(order.amount_cents, order.currency),
        "currency": order.currency,
    }


#: 状态的中文说法 —— 只用于**拼提示词给模型看**，接口对外仍然给英文枚举值。
_STATUS_ZH = {
    OrderStatus.PAID: "已付款未发货",
    OrderStatus.SHIPPED: "已发货在途",
    OrderStatus.DELIVERED: "已签收",
    OrderStatus.REFUNDING: "退款处理中",
    OrderStatus.CANCELLED: "已取消",
}


def render_order_facts(orders: Sequence[dict[str, Any]]) -> str:
    """把事实卡片渲染成提示词里的【订单信息】块。

    ⚠️ 与【参考资料】**分成两块**：订单是"事实"，条款是"规则"。
        混在一起模型会把事实当结论（"已签收 45 天" → 直接答"不能退"），
        而正确做法是"按质量问题 30 天条款 → 超过 → 转保修"。
    """
    if not orders:
        return ""
    lines: list[str] = []
    for item in orders:
        status = _STATUS_ZH.get(OrderStatus(item["status"]), str(item["status"]))
        names = "、".join(str(entry.get("name", "")) for entry in item.get("items") or [])
        lines.append(
            f"- 订单号 {item['order_no']}：{status}；下单 {item['placed_at']}；"
            f"签收 {item['delivered_at'] or '—'}"
            f"（距今 {item['days_since_delivery']} 天）；金额 {item['amount']}；商品 {names or '—'}"
        )
    return "\n".join(lines)


async def order_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    text: str,
    extra_order_no: str | None = None,
) -> tuple[list[dict[str, Any]], str, list[Any]]:
    """从一句话里捞单号 → 查库 → 返回（事实卡、提示词块、下单时间）。

    **Web 与飞书共用这一份**。为什么要抽出来：2026-10-26 前它只长在
    `api/chat.py` 里，结果**顾客侧（飞书）压根不查订单** —— 顾客问“我的单到哪了”
    只能得到一段政策。两边各写一份的话，下次又会只改一边。

    ⚠️ 过滤口径与 `find_order` 一致（**只按租户**，D5）。
    ⚠️ `extra_order_no` 是“上一轮已确认过的单号”（agent 的 scratchpad）；
       句子里没有单号时才用它兜底。
    """
    orders = await resolve_orders(session, tenant_id=tenant_id, text=text)
    # ⚠️ **这条就是 PLAN §13.6 的“回落路径也要读 scratchpad”**：
    #    顾客说“那保修呢”时，句子里没单号，但上一轮查过。
    if not orders and extra_order_no:
        found = await find_order(session, tenant_id=tenant_id, order_no=extra_order_no)
        orders = [found] if found is not None else []
    now = datetime.now(UTC)
    snapshots = [order_snapshot(order, now=now) for order in orders]
    return snapshots, render_order_facts(snapshots), [order.placed_at for order in orders]


__all__ = [
    "MAX_CANDIDATES",
    "days_since_delivery",
    "extract_order_candidates",
    "find_order",
    "format_amount",
    "order_context",
    "order_snapshot",
    "render_order_facts",
    "resolve_orders",
]
