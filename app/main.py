"""应用入口。

v1.0 已落地：
    运维面  ``/healthz``（compose healthcheck、CI 部署轮询、自动回滚判定都靠它）
            ``/metrics``（Prometheus 抓取）
    业务面  ``/api/v1/auth/*``          登录 + 当前用户
            ``/api/v1/conversations/*`` 会话列表 / 详情 / 新建（租户隔离样板）
            ``/api/v1/chat/*``          问答（SSE 流式，含引用溯源与拒答）
            ``/api/v1/documents/*``     知识库上传 / 列表（入队异步入库）
            ``/api/v1/reports``         只读评测报告（聚合数字，不跑评测）
            ``/api/v1/admin/*``         租户内管理端
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.router import api_router
from app.core.config import get_settings, warn_if_rerank_calibration_missing
from app.core.limits import RateLimitedError
from app.db.checks import warn_if_embedding_lock_mismatch, warn_if_fulltext_degraded
from app.db.session import dispose_engine
from app.rag.providers import aclose_http_client
from app.workers.queue import aclose_redis


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。

    ⚠️ 启动时先跑**两道自检**，它们治的是同一类病：**坏了但不报错**。

        · `app/core/config.py::warn_if_rerank_calibration_missing`
          —— 拒答阈值与 rerank 模型不配套时，会**全拒答或全放行**，接口照样 200。
        · `app/db/checks.py::warn_if_fulltext_degraded`
          —— 中文全文检索失效时**不报错、只是搜不到**。
        · `app/db/checks.py::warn_if_embedding_lock_mismatch`
          —— 换过 embedding 模型时，旧向量会被读侧静默忽略（**答案慢慢变差**）。

    ⚠️ 退出时**必须显式关连接池**：uvicorn 收到 SIGTERM 后如果连接还挂在池里，
        PostgreSQL 那边要等 TCP 超时才知道对端没了，期间连接数一直占着。
        容器滚动更新频繁时会把数据库的连接数堆满。
        HTTP 客户端（打模型 API 的那个）同理 —— 留着不关，优雅退出会被拖住。
    """
    warn_if_rerank_calibration_missing()
    await warn_if_fulltext_degraded()
    await warn_if_embedding_lock_mismatch()
    yield
    await aclose_http_client()
    await aclose_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    """构造 FastAPI 应用。

    用工厂函数而不是模块级裸 app，是为了测试里能按需构造（且将来好加 lifespan）。
    """
    settings = get_settings()

    app = FastAPI(
        title="grounded-qa",
        version=settings.app_version,
        description="多租户知识库问答平台 —— 客服是第一个业务场景",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["ops"], summary="存活探针")
    async def healthz() -> dict[str, str]:
        """存活探针。

        刻意**不**检查 PG / Redis：这个端点用于判断"进程是否该被重启"，
        依赖检查交给单独的 /readyz（后续补），否则依赖抖动会引发无谓重启。
        """
        return {
            "status": "ok",
            "env": settings.app_env,
            "version": settings.app_version,
        }

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """Prometheus 指标。格式由客户端库固定，不要在这里做业务加工。"""
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # 业务路由统一加版本前缀（前缀来自配置，不写死）
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.exception_handler(RateLimitedError)
    async def _rate_limited(_request: Request, exc: RateLimitedError) -> JSONResponse:
        """429：**说清"为什么"和"等多久"**。

        ⚠️ 为什么不用 `HTTPException(429, detail="...")`：
            那样响应体只有 `{"detail": "..."}`，前端拿不到 `scope` / `retry_after_s`，
            只能在"立刻重试"和"自己拍一个等待时间"之间二选一 —— 前者会把自己刷死。
        ⚠️ `Retry-After` 是 HTTP 标准头：让**非我们前端**的调用方（脚本、监控）
            也能知道该退避多久，不用解析我们的 JSON。
        """
        return JSONResponse(
            status_code=429,
            content=exc.payload(),
            headers={"Retry-After": str(exc.decision.retry_after_s)},
        )

    return app


app = create_app()
