"""队列方向的行为测试 —— 必须是 **FIFO**。

⚠️ 这里用一个按 Redis list 语义写的最小假实现，而不是连真 Redis：
    单测不依赖 docker；假实现只保留 `lpush` / `rpush` / `blpop` 三条，
    且方向与 Redis 一致（index 0 = 左端 = 队头）。
    语义依据：https://redis.io/docs/latest/commands/lpush/ 与 .../blpop/。
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.workers import queue as queue_module
from app.workers.queue import QUEUE_KEY, IngestJob, enqueue_ingest


class _FakeListRedis:
    """只实现队列用到的三条命令；`index 0` 就是 Redis 的 list 左端。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def lpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def blpop(self, keys: list[str], **_: int) -> tuple[str, str] | None:
        """`timeout` 用不上：假实现里队列非空就立刻返回，只为兼容 redis-py 的调用。"""
        for key in keys:
            items = self.lists.get(key)
            if items:
                return key, items.pop(0)
        return None


async def test_enqueue_is_fifo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeListRedis()
    monkeypatch.setattr(queue_module, "_redis", fake)

    jobs = [IngestJob(document_id=uuid.UUID(int=i), tenant_id=uuid.UUID(int=99)) for i in (1, 2, 3)]
    for job in jobs:
        await enqueue_ingest(job)

    popped = [await fake.blpop([QUEUE_KEY], timeout=1) for _ in range(3)]
    order = [uuid.UUID(json.loads(raw)["document_id"]).int for _, raw in popped if raw]

    assert order == [1, 2, 3]
