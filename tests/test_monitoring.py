"""运维看板与 HTTP 指标 —— 大部分是**纯单元测试**（不连库、不连 Prometheus）。

守三件事：
    · 中间件的 `path` 标签必须是**路由模板**，不是真实 URL（否则是基数炸弹）
    · Prometheus 的三种返回形状（vector / scalar / 空）都要能解析对
    · Prometheus 不可达时接口返回 200 + `available=false`，**不是 5xx**
      （看板挂了是"要显示出来的现状"，不是"接口故障"）
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.api import monitoring
from app.core.config import get_settings
from app.core.metrics import UNMATCHED, MetricsMiddleware
from tests.conftest import Account


def _sample(name: str, labels: dict[str, str]) -> float | None:
    """从**全局注册表**读一个样本值。

    ⚠️ 注册表是进程级的，别的测试也会往里写 —— 所以下面用**随机路由**制造唯一标签，
        断言只看"这条序列被加了几次"，不看全局总量。
    """
    return REGISTRY.get_sample_value(name, labels)


# ------------------------------------------------------------
# HTTP 中间件
# ------------------------------------------------------------
def test_middleware_labels_by_route_template_not_real_url() -> None:
    """两个不同的真实 URL、同一个路由模板 ⟹ **一条**时间序列，计 2 次。

    这条是整个指标设计的地基：用真实 URL 当标签，用户每建一个会话就多一条序列。
    """
    tag = uuid.uuid4().hex[:8]
    template = f"/probe-{tag}/{{item_id}}"
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get(template)
    async def probe(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    client = TestClient(app)
    assert client.get(f"/probe-{tag}/42").status_code == 200
    assert client.get(f"/probe-{tag}/99").status_code == 200

    labels = {"method": "GET", "path": template, "status": "200"}
    assert _sample("gq_http_requests_total", labels) == 2.0


def test_middleware_buckets_404_under_unmatched() -> None:
    """没匹配上路由的请求归到 `__unmatched__` —— **绝不**把真实路径写进标签。"""
    tag = uuid.uuid4().hex[:8]
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)
    client = TestClient(app)

    hidden = f"/no-such-route-{tag}/secret"
    assert client.get(hidden).status_code == 404

    labels = {"method": "GET", "path": UNMATCHED, "status": "404"}
    assert _sample("gq_http_requests_total", labels) is not None
    # 真实路径**不允许**出现在标签里
    assert (
        _sample("gq_http_requests_total", {"method": "GET", "path": hidden, "status": "404"})
        is None
    )


def test_middleware_records_500_and_does_not_swallow_error() -> None:
    """异常路径也要记 500 —— 否则"挂了"这件事在指标里根本不存在。"""
    tag = uuid.uuid4().hex[:8]
    template = f"/boom-{tag}"
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get(template)
    async def boom() -> None:
        raise RuntimeError("故意炸")

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(template).status_code == 500

    labels = {"method": "GET", "path": template, "status": "500"}
    assert _sample("gq_http_requests_total", labels) == 1.0


def test_middleware_does_not_touch_streaming_body() -> None:
    """响应体一个字节都不改 —— SSE 的流式全靠这一点。

    中间件只观察 `http.response.start`；用 `BaseHTTPMiddleware` 会把 body 包一层，
    症状是"流式变成一次性吐"，而且**本地看不出、过 nginx 才明显**。
    """
    from fastapi.responses import StreamingResponse

    tag = uuid.uuid4().hex[:8]
    template = f"/stream-{tag}"
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get(template)
    async def stream() -> StreamingResponse:
        async def _gen():
            yield b"event: a\n\n"
            yield b"event: b\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    client = TestClient(app)
    response = client.get(template)
    assert response.status_code == 200
    assert response.text == "event: a\n\nevent: b\n\n"


# ------------------------------------------------------------
# Prometheus 返回值的解析与并发查询
# ------------------------------------------------------------
def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_collect_parses_vector_and_strips_metric_name() -> None:
    """`by (outcome)` 这类结果要变成 `outcome=answered` 的标签，且**不含 `__name__`**。"""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "outcome" in query:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {
                                    "__name__": "gq_chat_outcome_total",
                                    "outcome": "answered",
                                },
                                "value": [1700000000.0, "3"],
                            },
                            {
                                "metric": {
                                    "__name__": "gq_chat_outcome_total",
                                    "outcome": "refused",
                                },
                                "value": [1700000000.0, "1"],
                            },
                        ],
                    },
                },
            )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    panels = await monitoring.collect("http://prom.test", transport=_transport(handler))

    # 卡片数量与顺序必须与 PANELS 一致（前端按 key 取）
    assert [panel.key for panel in panels] == [panel_def.key for panel_def in monitoring.PANELS]

    chat = next(panel for panel in panels if panel.key == "chat_24h")
    assert [(item.label, item.value) for item in chat.items] == [
        ("outcome=answered", 3.0),
        ("outcome=refused", 1.0),
    ]
    assert all(item.ts == 1700000000.0 for item in chat.items)

    # 没有数据的卡片是**空列表**，不是 0
    assert next(panel for panel in panels if panel.key == "api_up").items == []


async def test_collect_parses_scalar() -> None:
    """标量用的是 `[ts, value]`，没有外层 metric —— 解析分支不同，容易漏。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "scalar", "result": [1700000000.0, "0"]},
            },
        )

    panels = await monitoring.collect("http://prom.test", transport=_transport(handler))
    drift = next(panel for panel in panels if panel.key == "embedding_drift")
    assert len(drift.items) == 1
    assert drift.items[0].label == ""
    assert drift.items[0].value == 0.0


async def test_collect_raises_on_http_error() -> None:
    """一条查询失败就整体失败 —— 半份看板比没有看板更危险（会让人以为其他都正常）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(httpx.HTTPStatusError):
        await monitoring.collect("http://prom.test", transport=_transport(handler))


# ------------------------------------------------------------
# 接口层（走 HTTP，需要真库：鉴权要查用户表）
# ------------------------------------------------------------
@pytest.mark.integration
async def test_snapshot_reports_panels(client, login, make_account) -> None:
    """Prometheus 可查时：200 + `available=true` + 卡片齐全。"""
    account: Account = await make_account(label="mon")
    token = await login(account.email, account.password)

    async def _fake_collect(_base: str) -> list[monitoring.MonitorPanel]:
        return [
            monitoring.MonitorPanel(
                key=panel.key,
                group=panel.group,
                title=panel.title,
                unit=panel.unit,
                hint=panel.hint,
                items=[monitoring.MonitorItem(label="", value=1.0, ts=1700000000.0)],
            )
            for panel in monitoring.PANELS
        ]

    original = monitoring.collect
    monitoring.collect = _fake_collect
    try:
        response = await client.get(
            f"{get_settings().api_prefix}/monitoring/snapshot",
            headers=account.auth(token),
        )
    finally:
        monitoring.collect = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["error"] is None
    assert [panel["key"] for panel in body["panels"]] == [panel.key for panel in monitoring.PANELS]
    # 「不许裸数字」：口径说明和生成时间必须在场
    assert all(panel["hint"] for panel in body["panels"])
    assert body["generated_at_display"]


@pytest.mark.integration
async def test_snapshot_degrades_when_prometheus_is_down(client, login, make_account) -> None:
    """Prometheus 不可达 → **200 + available=false**，不是 5xx。

    看板取不到数据是"要显示的现状"（比如 monitoring profile 没启用），
    不该让前端整页报错。
    """
    account: Account = await make_account(label="mon-down")
    token = await login(account.email, account.password)

    async def _boom(_base: str) -> list[monitoring.MonitorPanel]:
        raise httpx.ConnectError("connection refused")

    original = monitoring.collect
    monitoring.collect = _boom
    try:
        response = await client.get(
            f"{get_settings().api_prefix}/monitoring/snapshot",
            headers=account.auth(token),
        )
    finally:
        monitoring.collect = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert body["panels"] == []
    assert "ConnectError" in body["error"]


@pytest.mark.integration
async def test_snapshot_requires_admin(client, login, make_account) -> None:
    """运维面（流量 / 成本 / 错误分布）只给管理员看。"""
    from app.db.models.user import UserRole

    account: Account = await make_account(role=UserRole.MEMBER, label="mon-member")
    token = await login(account.email, account.password)
    response = await client.get(
        f"{get_settings().api_prefix}/monitoring/snapshot",
        headers=account.auth(token),
    )
    assert response.status_code == 403
