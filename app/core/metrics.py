"""HTTP 与业务指标（Prometheus 侧的**统一出口**）。

为什么单独一个文件：指标是**跨模块的公共产出** —— 中间件、聊天路由、以后的后台任务
都要写它。塞进某个业务模块会让别的模块反向依赖那个模块。

⚠️ **标签基数（cardinality）是这类代码唯一的死法**，两条规矩：
    · `path` 一律用**路由模板**（`/api/v1/conversations/{conversation_id}`），
      **不是**真实 URL —— 否则用户每建一个会话就多一条时间序列，Prometheus 内存会慢慢涨死。
    · **不加 `user_id` / `tenant_id` 标签**：那等于把基数交给用户数据决定。
      按用户 / 租户的用量在 **Redis** 里（配额页读那儿），**不在** Prometheus 里。

⚠️ 为什么自己写纯 ASGI 中间件，而不用 `@app.middleware("http")`：
    那个装饰器背后是 `BaseHTTPMiddleware`，它会**包一层 body 的迭代器**再转发 ——
    而我们的 `/api/v1/chat` 是 SSE 长流。中间件一旦把 body 当"整体"处理，
    症状是"流式变成一次性吐"，而且**本地直连看不出、过 nginx 才明显**。
    纯 ASGI 只观察 `http.response.start` 那一帧，body 一个字节都不碰。
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import Counter, Histogram

Scope = dict[str, Any]
Message = dict[str, Any]

# 请求数：按 方法 / 路由模板 / 状态码
http_requests = Counter(
    "gq_http_requests_total",
    "HTTP 请求数（method=方法 path=路由模板 status=状态码）",
    ["method", "path", "status"],
)

# 耗时：分桶按本项目的实际量级定 —— P95 目标在秒级以内，30s 是 SSE 流的上限量级。
# ⚠️ 流式接口的耗时 = **整条流的时间**（用户感知的延迟），它会拉高全局 P95 ⟹
#    看板里把 `/api/v1/chat` 单独看，别混在普通请求里（见 monitoring.py 的口径说明）。
http_duration = Histogram(
    "gq_http_request_duration_seconds",
    "HTTP 请求耗时（秒，path=路由模板）",
    ["path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# 问答结果：**一次提问算一次**，与 HTTP 请求数不同（一次提问可能重连多次）
chat_outcome = Counter(
    "gq_chat_outcome_total",
    "问答结果分布（outcome=answered 正常回答 / refused 拒答 / error 出错）",
    ["outcome"],
)

# 没能匹配上任何路由的请求归到这一格。
# ⚠️ **不要**把真实 URL 当标签 —— 扫描器最喜欢打随机路径，那是基数炸弹。
UNMATCHED = "__unmatched__"


def record_chat_outcome(outcome: str) -> None:
    """记一次问答结果（answered / refused / error）。"""
    chat_outcome.labels(outcome=outcome).inc()


class MetricsMiddleware:
    """把每个 HTTP 请求记成「计数 + 耗时」。

    ⚠️ **状态码靠观察 `http.response.start` 拿到**，不靠返回值 ——
        ASGI 里没有"返回响应对象"这回事，应用是通过 `send` 回调把响应推出去的。
    ⚠️ 异常路径也要记（否则 500 全都不见了，而那正是最该看见的）：
        捕获后记 500 再**原样抛出** —— 中间件不许吞异常。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Any,
        send: Any,
    ) -> None:
        # 只关心 HTTP；lifespan / websocket 原样放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = {"code": 500}
        started = time.perf_counter()

        async def _send(message: Message) -> None:
            # 只**读**这一帧，绝不改写（改写会动到 SSE 的每一帧）
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            # ⚠️ `finally` 而不是 `except`：异常路径也要记 ——
            #    否则 500 全都不见了，而那正是最该看见的。异常照常往上抛，中间件不吞。
            # ⚠️ 路由模板在**请求处理完之后**才在 scope 里（Router 匹配时写进去的）。
            #    404 时它是 None ⟹ 归到 UNMATCHED，不用真实路径。
            route = scope.get("route")
            path = getattr(route, "path", None) or UNMATCHED
            method = scope.get("method", "?")
            http_requests.labels(method=method, path=path, status=str(status["code"])).inc()
            http_duration.labels(path=path).observe(time.perf_counter() - started)
