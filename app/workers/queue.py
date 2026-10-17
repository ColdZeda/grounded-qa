"""入库队列：**任务的形状 ＋ 谁去入队**。

⚠️ 队列就是 Redis 的一个 list，出队用 `BLPOP`（见 `app/workers/main.py`）。
    ~40 行能解决的事不引 Celery（AGENTS §7.1 约定 7）。

⚠️ **任务里只放主键，不放内容。**
    文件字节走**磁盘**（api 与 worker 共享的 `gq-uploads` 卷），不放消息里：
      · 一个 1 MB 的文件 base64 之后是 1.3 MB，塞进 Redis 要占内存、还要整条重传；
      · 消息一旦过大，Redis 的 `maxmemory-policy` 会在**没有任何报错**的情况下把它挤掉。

⚠️ **`enqueue` 用 `LPUSH`、消费用 `BLPOP`** —— 这是一个 **FIFO**（左边进、右边出）。
    两边都写成同一边的话就变成 LIFO，弱化成一个栈：连着传 10 个文件时，
    用户会看到**最后传的那个先做完**，而列表里排在第一个的一直在转圈。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis

from app.core.config import get_settings

#: 入库队列名。将来按租户分队列时，这里改成前缀 + tenant_id。
QUEUE_KEY: Final[str] = "gq:jobs:ingest"


@dataclass(frozen=True)
class IngestJob:
    """一条入库任务。

    ⚠️ 带上 `tenant_id` 不是为了"按租户取"，而是为了**日志里能定位** ——
        真出问题时第一个要回答的就是"哪一个租户的哪个文档"。
    """

    document_id: uuid.UUID
    tenant_id: uuid.UUID

    def dump(self) -> str:
        return json.dumps(
            {"document_id": str(self.document_id), "tenant_id": str(self.tenant_id)},
            ensure_ascii=False,
        )

    @classmethod
    def load(cls, raw: str) -> IngestJob:
        """反序列化。

        ⚠️ 这里**不做"宽容解析"**（缺字段就给默认值那种）。
            一条读不懂的消息只能来自代码版本不一致或人为塞错 —— 直接抛，
            让 worker 记一条错误日志然后跳过，**比猜出个半成品任务安全**。
        """
        payload = json.loads(raw)
        return cls(
            document_id=uuid.UUID(payload["document_id"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
        )


# ------------------------------------------------------------
# 连接
# ------------------------------------------------------------
#: 进程内单例。api 与 worker 各有一个，但**两条连接池互不相干**（本来就是两个容器）。
_redis: Redis | None = None


def get_redis() -> Redis:
    """取（并懒建）Redis 连接。

    ⚠️ 懒建而不是模块级 `Redis.from_url(...)`：模块级会在 import 时就建连接，
        而 import 发生在**应用还没读配置之前**，测试里也没法换掉。
    """
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def aclose_redis() -> None:
    """关闭连接池（应用/worker 退出时调用）。

    ⚠️ 不关的话，SIGTERM 之后进程还要等连接超时才能真正退出 ——
        滚动更新频繁时会把 Redis 的连接数堆起来。
    """
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def enqueue_ingest(job: IngestJob) -> None:
    """把任务推进队列。

    ⚠️ 抛异常就让它抛 —— 调用方（上传接口）必须**如实告诉用户"没排队成功"**。
        在这里吞掉异常，用户就会看到一个永远 `pending` 的文档。
    """
    await get_redis().lpush(QUEUE_KEY, job.dump())
