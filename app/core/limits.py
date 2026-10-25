"""限流与 token 配额 —— **成本熔断**，不是安全边界。

⚠️ 先把两件事分清：

    · **安全**（谁能调用）→ 鉴权（`app/core/security.py` / `app/api/deps.py`）
    · **成本**（一天能烧多少）→ 这里

## 两把尺子，量的不是一回事

| 尺子 | 单位 | 治什么 | 超了怎样 |
|---|---|---|---|
| **速率** | 次 / 分钟 / 用户 | 手抖连点、脚本刷、被爬 | 429 ＋ `Retry-After` |
| **配额** | token / 天 / 用户 | 一天的总账 | 429（**下一次**才拦，见下） |

⚠️ **配额是"软熔断"**：流式回答没法中途掐断（HTTP 状态码早就随第一个 `sources` 事件
    发出去了），所以做法是"**这次跑完、记账；下一次进来时发现已超 → 拒**"。
    代价是清楚的：**实际用量可能略超配额**（最多超一条回答的量）。
    这个偏差是**有意接受**的 —— 比"答到一半被切断"体验好得多。

## 为什么用 Redis 计数，不用进程内字典

单 worker 的进程内字典**重启就归零**，而配额是"一天的总账"—— 清账等于没有配额。
Redis 已经在栈里（入库队列也用它），代价只是每请求一次 `INCR`（本机 < 1 ms）。

## ⚠️ Redis 挂了怎么办：**放行**（fail-open）

这两把尺子治的是**成本**，不是安全。Redis 抖一下就把整站 429 掉，
等于把"缓存故障"升级成"服务不可用" —— 这个交换不划算。
所以 Redis 异常一律**放行 ＋ 打警告 ＋ 记 `gq_limit_failopen_total`**，
让监控上能看见"最近其实没在限流"。**静默放行才是真问题**，放行本身不是。

## 为什么按**用户**计数，而不是按 IP

nginx 已经按 IP 限了一道（`deploy/nginx/grounded-qa.conf` 的 `api_rl`），
那是**粗筛**（挡扫描器、挡单机刷）。这里这道才是**产品的**限额：

    · 一个办公室十个人走同一个出口 IP → 按 IP 算会互相拖累；
    · 一个人换个 IP 就绕过了按 IP 的限额 → 但他绕不过按用户的（token 里有 user id）。

📌 两道限额的**数值关系是有意的**：nginx 那道（120 次/分钟/IP）必须**明显宽于**
应用这道（30 次/分钟/用户），否则用户会先撞上 nginx 的 **HTML 错误页**，
而不是我们那条能说清"还有几秒"的 JSON。

## token 数从哪来：**模型 API 返回的 `usage`，不估算**

实测（2026-10-17，`deepseek-flash`）：请求里带 `stream_options.include_usage=true` 之后，
**流的最后一帧**带 `usage: {prompt_tokens, completion_tokens, total_tokens}`。
所以这里的数字是**真数**。估算（字数 ÷ 2 之类）在中文上误差 30%~50%，
用来做"一天的总账"会直接算错账 —— 不值得省那一个字段。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings

logger = logging.getLogger("grounded.limits")

#: 配额按**业务日**切（与容器里的 `TZ=Asia/Shanghai` 一致）。
#: ⚠️ 不按 UTC 切：UTC 的零点在北京是早上 8 点 —— 用户会觉得"早上刚开始用就没额度了"。
SHANGHAI: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

#: 固定窗口的长度。故意不做滑动窗口：成本多一次 zset 操作，收益只是消除
#: 窗口边界上的那一小段突发（最多 2 倍），对"防脚本刷"没有意义。
RATE_WINDOW_SECONDS: Final[int] = 60

Scope = Literal["rate", "quota"]

# ------------------------------------------------------------
# 指标（Prometheus）
# ------------------------------------------------------------
#: 被拒的请求。**按 scope 分**：rate 高说明有人刷，quota 高说明有人在烧钱。
_denied = Counter(
    "gq_limit_denied_total",
    "被限流 / 配额熔断拒绝的请求数",
    ["scope"],
)
#: Redis 出问题而放行的次数。**这个数不为 0 就说明"当前计量不可信"** ——
#: 它比"限流生效了多少次"更值得盯。（变量名比函数名少一个下划线，见 `_fail_open`。）
_failopen = Counter(
    "gq_limit_failopen_total",
    "因 Redis 不可用而放行的次数（计量不可信）",
    ["call"],
)
#: 累计 token（来自模型 API 的 usage，不是估算）。
_tokens = Counter("gq_tokens_total", "累计消耗的 token 数（模型 API 回报的 usage）")


# ------------------------------------------------------------
# 判定结果
# ------------------------------------------------------------
@dataclass(frozen=True)
class LimitDecision:
    """一次判定的结果。**路由只关心 `allowed`**，其余是给人看的话术。"""

    allowed: bool
    scope: Scope
    used: int
    limit: int
    retry_after_s: int
    #: 这次判定是"Redis 挂了所以放行"的产物。**不要忽略它** —— 见模块 docstring。
    failopen: bool = False

    @property
    def code(self) -> str:
        return "quota_exhausted" if self.scope == "quota" else "rate_limited"

    def message(self) -> str:
        if self.allowed:
            return ""
        if self.scope == "quota":
            return (
                f"今日 token 额度已用完（{self.used} / {self.limit}），"
                f"{human_delay(self.retry_after_s)}后重置（北京时间的零点）"
            )
        return f"请求太频繁（上限 {self.limit} 次/分钟），请 {self.retry_after_s} 秒后再试"


class RateLimitedError(Exception):
    """429。**由 `app/main.py` 的异常处理器翻译成 JSON ＋ `Retry-After` 头。**

    ⚠️ 不用 `HTTPException(429, detail="...")` 的原因：那样响应体只有 `{"detail": "..."}`，
        前端拿不到 `scope` / `retry_after_s` 这些**能用来做退避**的字段。
        只给一句话，前端就只能在"立刻重试"和"自己拍一个等待时间"之间二选一。
    """

    def __init__(self, decision: LimitDecision) -> None:
        super().__init__(decision.message())
        self.decision = decision

    def payload(self) -> dict[str, object]:
        return {
            "detail": self.decision.message(),
            "code": self.decision.code,
            "scope": self.decision.scope,
            "used": self.decision.used,
            "limit": self.decision.limit,
            "retry_after_s": self.decision.retry_after_s,
        }


def human_delay(seconds: int) -> str:
    """把秒数说成人话。**为了让人一眼知道"等多久"**，不是为了好看。

    ⚠️ 分界点放在 60 / 3600 秒，不是 90 / 5400 秒：
        「60 分钟」读起来比「1 小时」费劲，而配额重置最常落在"几十分钟"~"十几小时"这一段。
    """
    if seconds < 60:
        return f"{max(seconds, 1)} 秒"
    if seconds < 3600:
        return f"{round(seconds / 60)} 分钟"
    return f"{round(seconds / 3600)} 小时"


def ensure(decision: LimitDecision) -> LimitDecision:
    """放行就原样返回，拒绝就抛 `RateLimitedError`。"""
    if not decision.allowed:
        raise RateLimitedError(decision)
    return decision


# ------------------------------------------------------------
# Redis 键：窗口对齐
# ------------------------------------------------------------
def _rate_key(subject: str, now: datetime) -> tuple[str, int]:
    """`(键, 距本窗口结束还有几秒)`。

    ⚠️ 键里**带对齐后的时间戳**（精确到分钟），不是"用 TTL 当窗口"：
        靠 TTL 的话，窗口是从**第一次请求**开始算的（滑动），
        于是"每次隔 61 秒发一次"就永远不会触发限流。
    """
    local = now.astimezone(SHANGHAI)
    aligned = local.replace(second=0, microsecond=0)
    remaining = RATE_WINDOW_SECONDS - local.second
    return f"gq:rl:{subject}:{aligned:%Y%m%d%H%M}", max(remaining, 1)


def _quota_key(subject: str, now: datetime) -> tuple[str, int]:
    """`(键, 距今天零点还有几秒)`。"""
    local = now.astimezone(SHANGHAI)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"gq:quota:{subject}:{local:%Y%m%d}", int((tomorrow - local).total_seconds()) + 1


def _usage_key(subject: str, channel: str, now: datetime) -> str:
    """**按渠道的明细账**（只给人看，不参与任何判定）。

    ⚠️ 和 `_quota_key` 是两个键、两件事：配额键是"准入凭证"，它决定放不放行；
       这个键只回答"这个人的消耗里，网页 / 飞书私聊 / 飞书群聊各占多少"。
       **改这里绝不影响限流** —— 这是 2026-10-25 加渠道维度时定下的硬约束。
    """
    local = now.astimezone(SHANGHAI)
    return f"gq:usage:{subject}:{local:%Y%m%d}:{channel}"


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------
# 三个操作
# ------------------------------------------------------------
async def consume_rate(
    redis: Redis,
    *,
    subject: str,
    limit: int,
    now: datetime | None = None,
) -> LimitDecision:
    """速率：**先 +1、再判断**。

    ⚠️ 顺序很重要：先判后加的话，"已经被拒"的请求不计入，
        于是每第 N+1 个请求都会被判为"N 以内" —— 实际放行量是上限的两倍。
    """
    key, retry_after = _rate_key(subject, now or _now())
    try:
        used = int(await redis.incr(key))
        if used == 1:
            # ⚠️ `expire` 给窗口的 **2 倍**：万一某次 `expire` 没发出去（连接断了），
            #    键还能靠下一次请求重新带上过期时间，不至于永久留在 Redis 里。
            await redis.expire(key, RATE_WINDOW_SECONDS * 2)
    except RedisError as exc:
        return _fail_open("rate", limit, exc)

    allowed = used <= limit
    if not allowed:
        _denied.labels(scope="rate").inc()
    return LimitDecision(
        allowed=allowed, scope="rate", used=used, limit=limit, retry_after_s=retry_after
    )


async def read_quota(
    redis: Redis,
    *,
    subject: str,
    limit: int,
    now: datetime | None = None,
) -> LimitDecision:
    """配额：**只看、不加**。加是在回答结束之后（那时才知道花了多少）。"""
    key, retry_after = _quota_key(subject, now or _now())
    try:
        raw = await redis.get(key)
    except RedisError as exc:
        return _fail_open("quota", limit, exc)

    used = int(raw or 0)
    allowed = used < limit
    if not allowed:
        _denied.labels(scope="quota").inc()
    return LimitDecision(
        allowed=allowed, scope="quota", used=used, limit=limit, retry_after_s=retry_after
    )


async def peek_quota(
    redis: Redis,
    *,
    subject: str,
    now: datetime | None = None,
) -> int | None:
    """**只看账，什么都不改**：今天用了多少 token。

    ⚠️ 和 `read_quota` 的区别不是"读多少"，而是**有没有副作用**：
        `read_quota` 用于**准入判断**，超额时会给 `gq_limit_denied_total` 加一；
        配额页只是**看**，看一次就等于"被拒一次"是错的统计。
    ⚠️ 返回 `None` 表示**取不到**（Redis 挂了）—— 页面要显示 `—`，不能显示 0。
    """
    key, _ = _quota_key(subject, now or _now())
    try:
        raw = await redis.get(key)
    except RedisError:
        return None
    return int(raw or 0)


async def peek_rate(
    redis: Redis,
    *,
    subject: str,
    now: datetime | None = None,
) -> int | None:
    """**只看账**：本分钟已经用了几次（限流窗口内的计数）。同上，`None` = 取不到。"""
    key, _ = _rate_key(subject, now or _now())
    try:
        raw = await redis.get(key)
    except RedisError:
        return None
    return int(raw or 0)


async def peek_channel_usage(
    redis: Redis,
    *,
    subject: str,
    channels: Sequence[str],
    now: datetime | None = None,
) -> dict[str, int | None]:
    """**只看**：这个人在每个渠道上各花了多少 token。

    缺键 = 0（今天这个渠道没记过账）；Redis 挂了 = **全部 `None`**（页面显示 `—`）。
    ⚠️ 这两件事必须分开：0 是"没花"，`None` 是"不知道"。
    """
    moment = now or _now()
    out: dict[str, int | None] = {}
    try:
        for channel in channels:
            raw = await redis.get(_usage_key(subject, channel, moment))
            out[channel] = int(raw or 0)
    except RedisError:
        return dict.fromkeys(channels)
    return out


async def add_tokens(
    redis: Redis,
    *,
    subject: str,
    tokens: int,
    channel: str,
    now: datetime | None = None,
) -> None:
    """记账。**必须在回答结束之后调用**（`usage` 那时才有）。

    ⚠️ 调用点在 `_stream` 的 `finally` 里 —— 用户中途关掉页面时 token **照样花了**，
        不记的话，最费钱的那类请求（问大问题、看两眼就走）反而完全不进账。
    ⚠️ `channel` **必填**（2026-10-25）：漏传会让"分渠道之和 ≠ 总量"，
        而这种错**不会报错、只会让页面上的数字对不上** ⟹ 用必填参数把它变成编译期错误。
    """
    if tokens <= 0:
        return
    moment = now or _now()
    key, _ = _quota_key(subject, moment)
    try:
        used = int(await redis.incrby(key, tokens))
        if used == tokens:  # 今天第一次写 → 挂过期（2 天，跨零点也能查昨天的账）
            await redis.expire(key, 2 * 86_400)
        # 明细账单独一个键；**放在同一个 try 里** —— 配额记上了而明细没记上是可以接受的
        # （页面会显示"另有 N 未标注渠道"），但绝不能因为明细失败而回滚配额。
        detail = _usage_key(subject, channel, moment)
        detail_used = int(await redis.incrby(detail, tokens))
        if detail_used == tokens:
            await redis.expire(detail, 2 * 86_400)
    except RedisError as exc:
        _fail_open("add_tokens", 0, exc)
        return
    _tokens.inc(tokens)


def _fail_open(call: str, limit: int, exc: Exception) -> LimitDecision:
    """Redis 挂了 → 放行，但**留下痕迹**。见模块 docstring。

    ⚠️ 函数名（`_fail_open`）和指标变量名（`_failopen`）**故意错开一个下划线**：
        同名过一次 —— 函数把指标变量遮住了，一进异常分支就
        `AttributeError: 'function' object has no attribute 'labels'`，
        也就是"兜底逻辑自己先炸"，而且**只在 Redis 真挂的时候才会暴露**。
    """
    _failopen.labels(call=call).inc()
    logger.warning("限流/配额不可用，本次放行（%s）：%s: %s", call, type(exc).__name__, exc)
    return LimitDecision(
        allowed=True,
        scope="rate" if call == "rate" else "quota",
        used=0,
        limit=limit,
        retry_after_s=0,
        failopen=True,
    )


# ------------------------------------------------------------
# 门面：路由里只看得到几行
# ------------------------------------------------------------
class LimitGuard:
    """把"配置 + Redis + 判定"合成一个对象。

    ⚠️ 依赖注入的是 **redis**（`get_limits_redis`），不是这个类 ——
        测试里换掉 redis 就能跑真逻辑（窗口对齐、fail-open 全覆盖），
        而不是塞一个假 guard 把逻辑整段跳过。
    """

    __slots__ = ("_redis", "_settings")

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def chat_rate(self, user_id: uuid.UUID) -> LimitDecision:
        return await consume_rate(
            self._redis,
            subject=str(user_id),
            limit=self._settings.rate_limit_per_minute,
        )

    async def upload_rate(self, user_id: uuid.UUID) -> LimitDecision:
        return await consume_rate(
            self._redis,
            subject=f"upload:{user_id}",
            limit=self._settings.upload_rate_limit_per_minute,
        )

    async def quota(self, user_id: uuid.UUID) -> LimitDecision:
        return await read_quota(
            self._redis,
            subject=str(user_id),
            limit=self._settings.token_quota_per_day,
        )

    async def record_tokens(self, user_id: uuid.UUID, tokens: int, *, channel: str) -> None:
        """记账。`channel` 必填 —— 它是"这个人的消耗按渠道各占多少"的唯一来源。"""
        await add_tokens(self._redis, subject=str(user_id), tokens=tokens, channel=channel)

    async def channel_usage(
        self, user_id: uuid.UUID, channels: Sequence[str]
    ) -> dict[str, int | None]:
        return await peek_channel_usage(self._redis, subject=str(user_id), channels=channels)
