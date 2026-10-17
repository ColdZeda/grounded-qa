"""自研 mini worker —— Redis ``BLPOP`` 消费循环。

⚠️ **这不是 Celery，也不该是**（AGENTS.md §7.1 约定 7）：
    单机低频批处理场景，重型框架不划算。整个调度就是
    "阻塞取任务 → 处理 → 记结果"。

职责：文档解析 / 切分 / 向量化入库，**串行执行**，配合容器 ``mem_limit`` 兜底，
      避免一篇大文档把 2C2G 的机器打爆。

## 为什么只有**一个** worker 进程

    · 入库是 embedding API 密集型的，并发只会更快撞到对方的限流；
    · 串行意味着内存占用可预测（切完一篇再开下一篇），这在 2C2G 上是硬价值；
    · 启动时的「补队列」逻辑假设自己是唯一消费者（见 `_recover`）——
      开两个进程会互相把对方的任务重新入队。真要多开，先改这里。

## 一个任务的生命周期

    BLPOP 取出 ``{"document_id", "tenant_id"}``
      → 读库里那行 Document（**不看消息里的状态**，状态以库为准）
      → 从共享卷读字节
      → 切分 → 分批 embedding → 写 chunks → 状态改 READY
      → **删掉卷上的原始文件**（卷是传送带，不是存储）

⚠️ 任何一步失败都必须把 `Document.status` 落成 `FAILED` 并写 `error_message`。
    静默失败在这个功能里是最坏的结果：用户以为传进知识库了，之后问什么都拒答。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Final

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models.knowledge import Document, DocumentStatus
from app.db.session import dispose_engine, get_session_factory
from app.kb.ingest import IngestError, process_document
from app.kb.uploads import read_upload, remove_upload
from app.rag.pipeline import build_clients
from app.rag.providers import EmbeddingClient, aclose_http_client
from app.workers.queue import QUEUE_KEY, IngestJob, aclose_redis, enqueue_ingest, get_redis

logger = logging.getLogger("grounded.worker")

#: BLPOP 阻塞超时。不要设 0（永久阻塞）—— 那样收不到停止信号。
BLPOP_TIMEOUT_S: Final[int] = 5


async def handle_job(
    raw: str,
    *,
    embedder: EmbeddingClient | None,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """处理单个入库任务。

    ⚠️ `embedder` 由调用方**构造一次传进来**（AGENTS §7.1 约定 19）：
        每条消息都新建一个客户端，等于每秒都在重建 HTTP 连接池。
    """
    job = IngestJob.load(raw)
    logger.info("收到入库任务 doc=%s tenant=%s", job.document_id, job.tenant_id)

    async with factory() as session:
        document = await session.scalar(
            select(Document).where(
                Document.id == job.document_id,
                # ⚠️ 带上 tenant_id：消息里的租户和库里的必须一致。
                #    不一致说明有人伪造了消息 —— 那就**当它不存在**，别去动它。
                Document.tenant_id == job.tenant_id,
            )
        )
        if document is None:
            logger.warning("任务指向的文档不存在（已删除或租户不符）：%s", job.document_id)
            return

        try:
            data = await read_upload(job.document_id)
        except OSError as exc:
            # 文件没了（卷被重建、或被手工清过）—— 如实落库，别让界面一直转圈
            await _fail(session, document, f"读不到上传的文件（可能已失效，重新上传即可）：{exc}")
            return

        try:
            count = await process_document(
                session,
                document=document,
                data=data,
                embedder=embedder,
                settings=settings,
            )
        except IngestError:
            # `process_document` 已经把 FAILED + 原因落库了，这里只记一行日志
            logger.warning("入库失败 doc=%s", job.document_id)
        else:
            logger.info("入库完成 doc=%s 共 %d 块", job.document_id, count)
        finally:
            # ⚠️ 处理完就删（成功或失败都删）：卷是**传送带**，不是存储。
            #    留着的话，失败的文件会无声地永远堆在盘上；而这个文件已经没有再用处了
            #    —— 要重试就重新上传，字节一样会重新落盘。
            await remove_upload(job.document_id)


async def _fail(session: AsyncSession, document: Document, message: str) -> None:
    """把"还没走到切分就失败"的情况也落库（`process_document` 之外的失败）。"""
    document.status = DocumentStatus.FAILED
    document.error_message = message[:1000]
    await session.commit()
    logger.warning("入库失败 doc=%s：%s", document.id, message)


async def _recover(factory: async_sessionmaker[AsyncSession]) -> None:
    """worker 启动时把库里"卡住"的文档收拢。

    治的是两种**容器重启留下的孤儿**：

      · `PROCESSING` —— 上一个 worker 做到一半被 SIGKILL 了。它永远等不到结果，
        界面会一直转圈。**改判 FAILED**，并说明"重新上传即可重试"。
      · `PENDING` —— 消息可能已经丢了（Redis 没做持久化，重启会丢队列）。
        字节还在卷上，所以**重新入队**就能接着做。

    ⚠️ 只在**单 worker** 前提下成立（见模块 docstring）。
    """
    async with factory() as session:
        stuck = list(
            await session.scalars(
                select(Document).where(Document.status == DocumentStatus.PROCESSING)
            )
        )
        for document in stuck:
            document.status = DocumentStatus.FAILED
            document.error_message = "worker 重启，本次入库中断（重新上传即可重试）"
        pending = list(
            await session.scalars(select(Document).where(Document.status == DocumentStatus.PENDING))
        )
        jobs = [IngestJob(document_id=item.id, tenant_id=item.tenant_id) for item in pending]
        await session.commit()

    # ⚠️ **先提交、再入队**：反过来的话，入队成功而提交失败 = 消息指向一个 PENDING
    #    都没有的库，worker 取到就直接丢弃，用户永远等不到结果。
    for job in jobs:
        await enqueue_ingest(job)
        logger.info("补入队 doc=%s", job.document_id)

    for document in stuck:
        await remove_upload(document.id)

    if stuck or jobs:
        logger.info("启动收拢：中断 %d 个 → FAILED，补入队 %d 个", len(stuck), len(jobs))


async def run_forever() -> None:
    """主循环：阻塞取任务，直到收到停止信号。"""
    settings = get_settings()
    factory = get_session_factory()
    redis: Redis = get_redis()
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

    # embedding 客户端**只建一次**：它内部有连接池，每条消息重建等于每秒重开连接。
    # ⚠️ 没有 key 时是 `None` —— 不在这里退出，而是让每个任务落成带原因的 FAILED，
    #    这样界面能看见"为什么没进库"，比 worker 悄悄不启动好得多。
    _chat, embedder, _reranker = build_clients(settings)
    if embedder is None:
        logger.warning("没有配置 EMBEDDING_API_KEY：所有入库任务都会失败（原因会写进文档记录）")

    await _recover(factory)

    logger.info("mini worker 启动，监听队列 %s", QUEUE_KEY)
    try:
        while not stop.is_set():
            item = await redis.blpop([QUEUE_KEY], timeout=BLPOP_TIMEOUT_S)
            if item is None:
                # 超时返回，回到循环顶部 —— 这样才来得及检查停止信号
                continue
            _, raw = item
            try:
                await handle_job(raw, embedder=embedder, settings=settings, factory=factory)
            except Exception:
                # 单个任务失败**绝不能**拖垮 worker：记全栈，继续下一个
                logger.exception("任务处理失败，已跳过：%s", raw)
    finally:
        await aclose_http_client()
        await dispose_engine()
        await aclose_redis()
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
