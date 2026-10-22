"""参照时刻（`ref_time`）—— **用哪一天的眼光看知识库**。

⚠️ 为什么这件事不能省（v1.3 阶段 B2）：

    阶段 B 让检索只认"现行有效"的文档。这治好了"2023 版条款混进答案"，
    但带出一个新错：**老订单问保修，正确答案恰恰是当年那版**。
    2024-03 买的沙发，答复"家具主体 2 年"是错的 —— 当时生效的是 1 年。

⚠️ `ref_time` **只参与"资格"判断（SQL 的 `WHERE`），绝不进相关性分数**
    （`AGENTS §7.1` 约定 20）。混进分数就是"越新越相关"，且 badcase 归因不出来。

⚠️ 三级优先，**先确定、后猜测**：

    | 顺序 | 来源 | 可信度 | 什么时候有 |
    |---|---|---|---|
    | ① | 订单 `orders.placed_at` | 库里的**事实** | 这句话（或图里）能捞出本人的单号 |
    | ② | 问句里的显式日期 | 尽力而为，可能读错 | "我 2024 年 3 月买的" |
    | ③ | 今天 | 无信号 | 其余全部情况 |

⚠️ **没有任何信号时用"今天"，等于阶段 B 的行为** —— 不猜、也不反问用户要单号。
    反问会把"保修多久"这种泛问变成多一轮交互，而用户真在意的时候自己会说日期。

⚠️ 多张订单时取**最早**那张：我们只能挑一个时点，宁可让答案落在更老的政策边界上。
    把 2 年报成 1 年，用户会去核对；把 1 年报成 2 年，用户会被误导。

📌 **已知不覆盖**（都不做，写清楚免得当成漏了）：`去年` / `前年` / `上半年` 这类相对说法、
   两位年份（`24 年`）、"买了一个月"这种相对时长。读不出来就落到"今天"，不猜。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime


class RefTimeSource(enum.StrEnum):
    """`ref_time` 的**来源**。

    ⚠️ 必须跟着日期一起记（`Answer.extra` / 日志）—— 只留一个日期的话，
        事后分不清"这是订单的下单日"还是"模型把一句话读成了日期"，
        而这两者的可信度差一个数量级。
    """

    ORDER = "order"
    QUESTION = "question"
    #: 评测冻结的值（`evals/run.py --ref-time`）。**不是产品行为**，只为可复现。
    FROZEN = "frozen"
    #: 今天。**= 没收到信号**，行为与阶段 B 完全一致。
    TODAY = "today"


@dataclass(frozen=True)
class RefTime:
    """一次问答所采用的参照时刻。"""

    value: date
    source: RefTimeSource

    @property
    def informed(self) -> bool:
        """有没有真的收到信号（`TODAY` = 没收到）。

        决定**要不要在提示词里加【时间基准】那一块**：没信号时加它只会让模型
        以为"用户说了个日期"，反而多一层猜测。
        """
        return self.source is not RefTimeSource.TODAY

    def as_extra(self) -> dict[str, str]:
        """进 `Answer.extra` / 日志的结构。"""
        return {"ref_time": self.value.isoformat(), "ref_time_source": self.source.value}


#: 中文写法：`2024年3月15日` / `2024年3月` / `2024年`。
#: ⚠️ `(?<!\d)` 防止从一长串数字中间咬下一段（`20240315` 不该被读成日期）。
_CN_DATE = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*年"
    r"(?:\s*(?P<month>\d{1,2})\s*月"
    r"(?:\s*(?P<day>\d{1,2})\s*[日号])?)?"
)

#: 分隔符写法：`2024-03-15` / `2024/3/15` / `2024-03`。
#: ⚠️ **只认 `-` 与 `/`**，不认 `.`：`1.5` 这种小数太容易误伤。
_SPLIT_DATE = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})[-/](?P<month>\d{1,2})(?:[-/](?P<day>\d{1,2}))?(?!\d)"
)


def _to_date(match: re.Match[str]) -> date | None:
    """把一次匹配变成 `date`；月/日缺省成 1，非法值（`2024-02-30`）返回 `None`。"""
    try:
        return date(
            int(match.group("year")),
            int(match.group("month") or 1),
            int(match.group("day") or 1),
        )
    except ValueError:
        return None


def parse_explicit_date(text: str) -> date | None:
    """从一句话里捞出**显式日期**（取出现位置最靠前的那个）。读不出来 → `None`。

    ⚠️ 只认"年月日"这类确定写法，**不做相对时间推算** —— "去年冬天"能推出一个日期，
        但推错的时候用户完全无从察觉，而读不出来还会老实退到"今天"。
    """
    best: tuple[int, date] | None = None
    for pattern in (_CN_DATE, _SPLIT_DATE):
        for match in pattern.finditer(text or ""):
            parsed = _to_date(match)
            if parsed is None:
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), parsed)
            break  # 同一形态只看第一个就够了，再多也没有更可信的
    return best[1] if best else None


def _as_date(stamp: datetime | date) -> date:
    """`datetime` / `date` 统一成 `date`。

    ⚠️ 时区不做换算：`orders.placed_at` 是 `timestamptz`，读出来通常带 UTC 时区，
        跨零点时可能差一天。政策区间以**年**计，一天不影响归属 —— 与其在这里引入
        一套时区配置（又多一个能配错的地方），不如记下这个已知的一日误差。
    """
    return stamp.date() if isinstance(stamp, datetime) else stamp


def resolve_ref_time(
    *,
    question: str = "",
    order_placed_at: Sequence[datetime | date] = (),
    today: date | None = None,
) -> RefTime:
    """定这次问答的参照时刻。见模块 docstring 的三级优先。

    Args:
        question: 用户原话（**不是**拼了图片描述的检索 query）。
        order_placed_at: 这次问话能对上的订单的下单时间。空 = 没查到订单。
        today: 便于测试注入；不传取系统当天。
    """
    stamps = [_as_date(item) for item in order_placed_at]
    if stamps:
        return RefTime(min(stamps), RefTimeSource.ORDER)

    explicit = parse_explicit_date(question)
    if explicit is not None:
        return RefTime(explicit, RefTimeSource.QUESTION)

    return RefTime(today or date.today(), RefTimeSource.TODAY)


__all__ = [
    "RefTime",
    "RefTimeSource",
    "parse_explicit_date",
    "resolve_ref_time",
]
