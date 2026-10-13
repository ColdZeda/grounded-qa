"""应用入口。

v1.0 骨架阶段只保证两件事可用：
    1. ``/healthz`` —— compose healthcheck、CI 部署 job 轮询、失败回滚判定都靠它
    2. ``/metrics`` —— Prometheus 抓取（AGENTS.md §5.2 部署前检查项）

业务路由（会话 / 聊天 / 知识库 / 评测报告）在后续里程碑按 app/api/ 分模块补。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import get_settings


def create_app() -> FastAPI:
    """构造 FastAPI 应用。

    用工厂函数而不是模块级裸 app，是为了测试里能按需构造（且将来好加 lifespan）。
    """
    settings = get_settings()

    app = FastAPI(
        title="grounded-qa",
        version=settings.app_version,
        description="多租户知识库问答平台 —— 客服是第一个业务场景",
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

    return app


app = create_app()
