"""够用的内存版 Redis —— **只实现我们真正用到的那几条命令**。

⚠️ 为什么不用 `fakeredis` 那个包：这里只要 `incr/incrby/expire/get` 四条，
    自己写 40 行比多一个依赖便宜（也更容易看清"我们的代码到底依赖了什么语义"）。

⚠️ **没有这个假实现会怎样**：集成测试会去连**真的** Redis，
    于是整套测试共享同一个限流计数器 —— 跑几十个聊天用例就会把
    30 次/分钟的额度用完，后面的用例开始 429，**看起来像业务 bug，其实是测试互相踩**。
    这类 flaky 最难查（单跑都过、连跑就挂）。
"""

from __future__ import annotations

import time

from redis.exceptions import RedisError


class FakeRedisUnavailable(RedisError):
    """模拟"Redis 连不上"。用来验证 fail-open。"""


class FakeRedis:
    """内存字典，带过期时间。**键的语义与真 Redis 一致**（过期后 `get` 返回 `None`）。"""

    def __init__(self, *, broken: bool = False) -> None:
        self.data: dict[str, int] = {}
        self.expires_at: dict[str, float] = {}
        #: 置 True 后所有命令都抛 `RedisError` —— 验证 fail-open 用。
        self.broken = broken
        #: 调用流水，便于断言"到底访问了几次 Redis"。
        self.calls: list[tuple[str, str]] = []

    # ---------- 内部 ----------
    def _guard(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if self.broken:
            raise FakeRedisUnavailable("fake redis is down")

    def _live(self, key: str) -> bool:
        deadline = self.expires_at.get(key)
        if deadline is None:
            return True
        if time.monotonic() >= deadline:
            self.data.pop(key, None)
            self.expires_at.pop(key, None)
            return False
        return True

    # ---------- 我们用到的那几条 ----------
    async def incr(self, key: str) -> int:
        self._guard("incr", key)
        value = self.data.get(key, 0) + 1 if self._live(key) else 1
        self.data[key] = value
        return value

    async def incrby(self, key: str, amount: int) -> int:
        self._guard("incrby", key)
        value = (self.data.get(key, 0) if self._live(key) else 0) + amount
        self.data[key] = value
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self._guard("expire", key)
        if key not in self.data:
            return False
        self.expires_at[key] = time.monotonic() + seconds
        return True

    async def get(self, key: str) -> int | None:
        self._guard("get", key)
        if not self._live(key):
            return None
        return self.data.get(key)

    # ---------- 测试便利 ----------
    def seed(self, key: str, value: int) -> None:
        """直接铺一个计数（省得为了造"已用满"而循环几百次）。"""
        self.data[key] = value
