"""告警搬运工 —— **纯单元测试**（不连 Prometheus、不连真的群机器人、不连库）。

守四件事：
    · **没配 webhook 就什么都不做**（不能因为"可选功能没配"就让服务起不来）
    · **先推成功、再记账**（反了就会把推送失败的那条永久静默）
    · pending **不算告警**（否则每抖一下就报一次）
    · 消息里**只有**服务名 / 指标名 / 当前值 / 时间
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.alerts import (
    ACTIVE_KEY,
    Alert,
    AlertShipper,
    build_payload,
    format_message,
    parse_alerts,
    start_shipper,
    webhook_kind,
)
from app.core.config import Settings
from tests.fakeredis import FakeRedis

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"


def _settings(**overrides: Any) -> Settings:
    """构造 Settings。⚠️ 不读真 `.env`，`app_env` 保持 dev 以免撞上生产密钥校验。"""
    base: dict[str, Any] = {
        "app_env": "dev",
        "alert_webhook_url": FEISHU_URL,
        "prometheus_url": "http://prom.test",
    }
    base.update(overrides)
    return Settings(**base)


def _prom_payload(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"alerts": list(alerts)}}


def _firing(
    name: str = "ApiScrapeDown", metric: str = "up", value: str = "0e+00"
) -> dict[str, Any]:
    return {
        "labels": {"alertname": name, "job": "grounded-api", "metric": metric},
        "annotations": {"summary": "随便写的文案，不该被用"},
        "state": "firing",
        "activeAt": "2026-10-19T00:00:00Z",
        "value": value,
    }


# ------------------------------------------------------------
# 报文格式：按域名自动判断
# ------------------------------------------------------------
def test_webhook_kind_by_domain() -> None:
    assert webhook_kind(FEISHU_URL) == "feishu"
    assert webhook_kind(WECOM_URL) == "wecom"
    # 认不出来的域名按企微（text 报文最通用）兜底 —— **不报错**
    assert webhook_kind("https://example.com/hook") == "wecom"


def test_build_payload_shapes() -> None:
    feishu = build_payload(FEISHU_URL, "hi")
    assert feishu["msg_type"] == "text"
    assert feishu["content"]["text"] == "hi"

    wecom = build_payload(WECOM_URL, "hi")
    assert wecom["msgtype"] == "text"
    assert wecom["text"]["content"] == "hi"


# ------------------------------------------------------------
# 解析：pending 不算、只用 labels 的白名单
# ------------------------------------------------------------
def test_parse_alerts_skips_pending() -> None:
    pending = _firing()
    pending["state"] = "pending"
    alerts = parse_alerts(_prom_payload(pending))
    assert alerts == []


def test_parse_alerts_takes_only_whitelisted_labels() -> None:
    # annotations 里塞了"用户数据"，**绝不能**进消息
    payload = _prom_payload(
        {
            "labels": {"alertname": "ApiScrapeDown", "job": "grounded-api", "metric": "up"},
            "annotations": {"summary": "用户问了什么？张三的手机号 13800000000"},
            "state": "firing",
            "value": "1e+00",
        }
    )
    (alert,) = parse_alerts(payload)
    assert alert == Alert(name="ApiScrapeDown", service="grounded-api", metric="up", value="1")
    text = format_message(alert, resolved=False)
    assert "13800000000" not in text


def test_format_message_has_only_service_metric_value_time() -> None:
    alert = Alert(name="ApiScrapeDown", service="grounded-api", metric="up", value="0")
    text = format_message(alert, resolved=False)
    assert "grounded-api" in text
    assert "up = 0" in text
    assert "时间：" in text
    # resolved 不该带值（那时 Prometheus 已经不返回它了）
    resolved = format_message(alert, resolved=True)
    assert "已恢复" in resolved
    assert "= 0" not in resolved


# ------------------------------------------------------------
# 一轮轮询：推送 + 去重
# ------------------------------------------------------------
class _Recorder:
    """假 webhook：记下每一份报文。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/alerts"):
            raise AssertionError("这个假 handler 只该收到 webhook 请求")
        self.sent.append(request.read().decode("utf-8"))
        if self.fail:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"code": 0})


async def test_poll_pushes_once_then_dedupes() -> None:
    """同一条告警连轮两次，**只推一次** —— 这就是去重的全部意义。"""
    redis = FakeRedis()
    recorder = _Recorder()
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))

    async with AlertShipper(_settings(), redis, client=client) as shipper:

        async def _fetch() -> list[Alert]:
            return parse_alerts(_prom_payload(_firing()))

        shipper._fetch_firing = _fetch  # type: ignore[method-assign]

        assert await shipper.poll_once() == (1, 0)
        assert await shipper.poll_once() == (0, 0)

    assert len(recorder.sent) == 1
    assert "ApiScrapeDown" in recorder.sent[0]
    assert await redis.smembers(ACTIVE_KEY) == {"ApiScrapeDown|grounded-api|up"}


async def test_poll_pushes_resolved_and_forgets() -> None:
    """告警消失 → 推一条「已恢复」，并把它从去重集合里删掉（否则永不重报）。"""
    redis = FakeRedis()
    await redis.sadd(ACTIVE_KEY, "ApiScrapeDown|grounded-api|up")
    recorder = _Recorder()
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))

    async with AlertShipper(_settings(), redis, client=client) as shipper:

        async def _fetch() -> list[Alert]:
            return []

        shipper._fetch_firing = _fetch  # type: ignore[method-assign]

        assert await shipper.poll_once() == (0, 1)

    assert len(recorder.sent) == 1
    assert "已恢复" in recorder.sent[0]
    assert await redis.smembers(ACTIVE_KEY) == set()


async def test_poll_does_not_mark_when_push_fails() -> None:
    """⚠️ 推送失败**不能**记账 —— 反了的话这条告警就永远不再推（静默丢失）。

    这一条是整个模块最要紧的断言：它保证"下一轮会重试"是**真的能重试**。
    """
    redis = FakeRedis()
    recorder = _Recorder(fail=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))

    async with AlertShipper(_settings(), redis, client=client) as shipper:

        async def _fetch() -> list[Alert]:
            return parse_alerts(_prom_payload(_firing()))

        shipper._fetch_firing = _fetch  # type: ignore[method-assign]

        try:
            await shipper.poll_once()
        except httpx.HTTPStatusError:
            pass
        else:  # pragma: no cover - 走到这里说明代码把异常吞了
            raise AssertionError("推送失败必须抛出去，让调用方（run 循环）下一轮重试")

    assert await redis.smembers(ACTIVE_KEY) == set()


async def test_poll_does_not_mark_when_webhook_rejects_with_200() -> None:
    """⚠️ **HTTP 200 也可能是拒绝**：飞书 / 企微把错误放在 body 的 code 里。

    2026-10-19 推演到的形状：IP 白名单没放行 / 关键词不匹配 →
    `200 {"code":19021,...}`。只看状态码就会记成"已投递"，
    于是**消息没到、而且永远不会重推**（静默丢失）。
    """
    redis = FakeRedis()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})
        )
    )

    async with AlertShipper(_settings(), redis, client=client) as shipper:

        async def _fetch() -> list[Alert]:
            return parse_alerts(_prom_payload(_firing()))

        shipper._fetch_firing = _fetch  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="19021"):
            await shipper.poll_once()

    # 关键断言：**没有记账** ⟹ 下一轮还会重试
    assert await redis.smembers(ACTIVE_KEY) == set()


async def test_poll_marks_when_wecom_returns_errcode_zero() -> None:
    """企微的成功形状是 `{"errcode":0}` —— 也要认，别把成功当失败。"""
    redis = FakeRedis()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"errcode": 0}))
    )

    async with AlertShipper(
        _settings(alert_webhook_url=WECOM_URL), redis, client=client
    ) as shipper:

        async def _fetch() -> list[Alert]:
            return parse_alerts(_prom_payload(_firing()))

        shipper._fetch_firing = _fetch  # type: ignore[method-assign]
        assert await shipper.poll_once() == (1, 0)

    assert await redis.smembers(ACTIVE_KEY) == {"ApiScrapeDown|grounded-api|up"}


# ------------------------------------------------------------
# 启动：没配 URL 就不起任务
# ------------------------------------------------------------
async def test_start_shipper_is_noop_without_webhook() -> None:
    task = start_shipper(_settings(alert_webhook_url=""), FakeRedis())
    assert task is None


async def test_start_shipper_returns_task_when_configured() -> None:
    task = start_shipper(
        _settings(alert_webhook_url=WECOM_URL, alert_poll_interval_s=3600), FakeRedis()
    )
    assert task is not None
    task.cancel()
