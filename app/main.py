"""应用入口。

v1.0 已落地：
    运维面  ``/healthz``（compose healthcheck、CI 部署轮询、自动回滚判定都靠它）
            ``/metrics``（Prometheus 抓取）
    业务面  ``/api/v1/auth/*``          登录 + 当前用户
            ``/api/v1/conversations/*`` 会话列表 / 详情 / 新建（租户隔离样板）

后续里程碑按 `app/api/` 分模块继续补（聊天 SSE / 知识库 / 评测报告）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import dispose_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。

    ⚠️ 退出时**必须显式关连接池**：uvicorn 收到 SIGTERM 后如果连接还挂在池里，
        PostgreSQL 那边要等 TCP 超时才知道对端没了，期间连接数一直占着。
        容器滚动更新频繁时会把数据库的连接数堆满。
    """
    yield
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

    return app


app = create_app()
