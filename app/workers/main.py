"""自研 mini worker —— Redis ``BLPOP`` 消费循环。

⚠️ **这不是 Celery，也不该是**（AGENTS.md §7.1 约定 7）：
    单机低频批处理场景，重型框架不划算。整个调度就是
    "阻塞取任务 → 处理 → 记结果"。

职责：文档解析 / 切分 / 向量化入库，**串行执行**，配合容器 ``mem_limit`` 兜底，
      避免一篇大文档把 2C2G 的机器打爆。

骨架阶段：主循环、信号处理、单任务错误隔离已就位；具体处理逻辑留 TODO(v1.0)。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Final

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger("grounded.worker")

# 入库队列名。将来按租户分队列时，这里改成前缀 + tenant_id。
QUEUE_KEY: Final[str] = "gq:jobs:ingest"

# BLPOP 阻塞超时。不要设 0（永久阻塞）—— 那样收不到停止信号。
BLPOP_TIMEOUT_S: Final[int] = 5


async def handle_job(raw: str) -> None:
    """处理单个入库任务。

    TODO(v1.0)：解析 → 切分 → 嵌入（走 API）→ 写 pgvector。
    现阶段只记录日志，保证主循环可跑、可观测。
    """
    logger.info("收到入库任务：%s", raw)


async def run_forever() -> None:
    """主循环：阻塞取任务，直到收到停止信号。"""
    settings = get_settings()
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        logger.info("收到停止信号，退出中")
        stop.set()

    # Windows 不支持 add_signal_handler；worker 跑在 Linux 容器里，
    # 但本地直接跑调试时不该因此崩掉。
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - 仅 Windows 本地调试
            signal.signal(sig, _request_stop)

    logger.info("mini worker 启动，监听队列 %s", QUEUE_KEY)
    try:
        while not stop.is_set():
            item = await redis.blpop([QUEUE_KEY], timeout=BLPOP_TIMEOUT_S)
            if item is None:
                # 超时返回，回到循环顶部 —— 这样才来得及检查停止信号
                continue
            _, raw = item
            try:
                await handle_job(raw)
            except Exception:
                # 单个任务失败**绝不能**拖垮 worker：记全栈，继续下一个
                logger.exception("任务处理失败，已跳过：%s", raw)
    finally:
        await redis.aclose()
        logger.info("mini worker 已退出")


def main() -> None:
    """进程入口（compose 里跑 ``python -m app.workers.main``）。"""
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
