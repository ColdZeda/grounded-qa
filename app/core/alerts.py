"""告警搬运工：把 Prometheus 的 **firing 状态**推到人手上。

⚠️ **为什么不用 Alertmanager**：
    它要再加一个容器（≈40 MB）＋ 一整套 route/receiver/inhibit 配置，而我们要的只有
    「新出现的 firing 推一次、恢复了再推一次」。2C2G 上这是**用一整套路由系统换两个 if**。
    代价说清楚：**没有分组、没有静默、没有抑制、没有重试队列** —— 见文末"已知的将就"。

⚠️ **没配 `ALERT_WEBHOOK_URL` 就什么都不做**（只打一行「告警未配置」）。
    "可选功能缺失"不能变成"服务起不来"：告警是**旁路**，业务随时要能用。

⚠️ **内容只带 服务名 + 指标名 + 当前值 + 时间**。
    Prometheus 那边的 annotations 可能被人写成任意文案，所以这里**不复用 annotations**，
    只从 labels 里挑白名单字段 —— 粘性越少，越不可能把用户数据带出去。

## 去重靠 Redis 的一个 set（`gq:alert:active`）

    每轮：取 Prometheus 当前 firing 的指纹集合 `firing`
    · `firing - active` → **新告警**，推一条 firing，然后加进 active
    · `active - firing` → **已恢复**，推一条 resolved，然后从 active 移除

    ⚠️ **先推成功再改 active**：反过来的话，推失败的那次会被当成"已经通知过"，
       于是这条告警**永远不再推**（静默丢失比重复推送严重得多）。
    ⚠️ set 放在 Redis 而不是进程内存：api 重启后仍然知道"哪些已经推过"，
       否则每次部署都会把还在 firing 的告警**重推一遍**。

## 已知的将就

· **没有重试队列**：推送失败就在下一轮（60 秒后）自然重试一次。
· **没有静默**：要临时闭嘴就清掉 `gq:alert:active` 或把阈值调高。
· resolved 只带服务名与指标名（**不查历史值**）：它要回答的是"好了没"，不是"多少"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.limits import SHANGHAI

logger = logging.getLogger("grounded.alerts")

#: 已知正在 firing 的告警指纹。**进程内不存**，全部落 Redis（理由见模块说明）。
ACTIVE_KEY: Final[str] = "gq:alert:active"

#: 一轮最多推几条。防止一次大故障（比如整机全挂）把 webhook 打爆 ——
#: 群机器人都有限流，超了会被静默丢弃，反而什么都没有。
MAX_PUSH_PER_ROUND: Final[int] = 10

#: 单次 HTTP 的超时。群机器人接口很慢时不要让告警任务挂住。
_TIMEOUT_S: Final[float] = 5.0

#: 认得的域名 → 报文格式。**按域名判断，不问用户选哪个**。
FEISHU_HOST: Final[str] = "open.feishu.cn"
WECOM_HOST: Final[str] = "qyapi.weixin.qq.com"


@dataclass(frozen=True)
class Alert:
    """一条告警的**最小信息集**（够写成一句话，且不夹带任何业务内容）。"""

    name: str
    service: str
    metric: str
    value: str

    @property
    def fingerprint(self) -> str:
        """去重用的指纹。

        ⚠️ 用 `|` 分隔而不是拼在一起：resolved 时要从指纹里**把服务名和指标名拆回来**
            （那时 Prometheus 已经不返回这条告警了）。
        """
        return f"{self.name}|{self.service}|{self.metric}"


def _fmt_value(raw: str) -> str:
    """Prometheus 给的值是字符串（`0e+00` / `5.5e+01`），转成人能读的。

    认不出来就**原样带出去** —— 猜一个数字比显示原始字符串更糟。
    """
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if number.is_integer() and abs(number) < 1e9:
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def webhook_kind(url: str) -> str:
    """按 URL 域名判断报文格式：`feishu` / `wecom` / `wecom`（认不出时按企微兜底）。"""
    low = url.lower()
    if FEISHU_HOST in low:
        return "feishu"
    # 企微，以及"认不出来但大概率是通用 webhook"的情况 —— text 报文最通用
    return "wecom"


def build_payload(url: str, text: str) -> dict[str, Any]:
    """拼报文。两家都是 `text` 类型 —— 卡片好看但字段容易随他们改版而漂。"""
    if webhook_kind(url) == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"msgtype": "text", "text": {"content": text}}


def format_message(alert: Alert, *, resolved: bool) -> str:
    """一句话告警。**只有服务名 / 指标名 / 当前值 / 时间**，没有别的。"""
    now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    if resolved:
        return (
            f"✅ 告警已恢复｜{alert.name}\n"
            f"服务：{alert.service}\n"
            f"指标：{alert.metric}\n"
            f"时间：{now}（北京时间）"
        )
    return (
        f"🚨 告警 firing｜{alert.name}\n"
        f"服务：{alert.service}\n"
        f"指标：{alert.metric} = {alert.value}\n"
        f"时间：{now}（北京时间）"
    )


def parse_alerts(payload: dict[str, Any]) -> list[Alert]:
    """从 Prometheus `/api/v1/alerts` 的响应里挑出**正在 firing** 的告警。

    ⚠️ 要过滤 `state`：那个接口**也返回 `pending`**（还没到 `for:` 的时长）。
        把 pending 当成告警推出去 = **每抖一下就报一次**。
    ⚠️ 只用 labels 里的白名单字段（`metric` 是我们自己在规则里加的），**不用 annotations**。
    """
    out: list[Alert] = []
    for item in (payload.get("data") or {}).get("alerts") or []:
        if str(item.get("state") or "").lower() != "firing":
            continue
        labels = item.get("labels") or {}
        out.append(
            Alert(
                name=str(labels.get("alertname") or "unknown"),
                service=str(labels.get("job") or "—"),
                metric=str(labels.get("metric") or "—"),
                value=_fmt_value(str(item.get("value") or "")),
            )
        )
    return out


def _split_fingerprint(fp: str) -> Alert:
    """把指纹拆回一条 `Alert`（resolved 时用；值未知，留空）。"""
    parts = fp.split("|")
    name, service, metric = ([*parts, "unknown", "—", "—"])[:3]
    return Alert(name=name, service=service, metric=metric, value="")


class AlertShipper:
    """轮询 Prometheus → 推 webhook。**一次 `poll_once()` 不需要任何后台任务就能测**。"""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self._client = client
        self._own_client = client is None

    async def __aenter__(self) -> AlertShipper:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT_S)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 内部 ----------
    async def _fetch_firing(self) -> list[Alert]:
        assert self._client is not None, "AlertShipper 必须先进 async with"
        base = self.settings.prometheus_url.rstrip("/")
        response = await self._client.get(f"{base}/api/v1/alerts")
        response.raise_for_status()
        return parse_alerts(response.json())

    async def _push(self, text: str) -> None:
        assert self._client is not None
        url = self.settings.alert_webhook_url.strip()
        response = await self._client.post(url, json=build_payload(url, text))
        response.raise_for_status()

    # ---------- 主流程 ----------
    async def poll_once(self) -> tuple[int, int]:
        """跑一轮。返回 `(新告警数, 恢复数)`。

        ⚠️ 任何一个推送失败都会**中断这一轮**（剩下的留给下一轮）——
            但**已经成功推送的已经写进 Redis**，不会重复推。
        """
        firing = {alert.fingerprint: alert for alert in await self._fetch_firing()}
        known: set[str] = set(await self.redis.smembers(ACTIVE_KEY))

        new_keys = sorted(set(firing) - known)
        gone_keys = sorted(known - set(firing))

        pushed_new = 0
        for key in new_keys[:MAX_PUSH_PER_ROUND]:
            await self._push(format_message(firing[key], resolved=False))
            await self.redis.sadd(ACTIVE_KEY, key)
            pushed_new += 1
            logger.warning("告警已推送：%s", firing[key].name)

        pushed_gone = 0
        for key in gone_keys[:MAX_PUSH_PER_ROUND]:
            await self._push(format_message(_split_fingerprint(key), resolved=True))
            await self.redis.srem(ACTIVE_KEY, key)
            pushed_gone += 1

        if len(new_keys) > MAX_PUSH_PER_ROUND or len(gone_keys) > MAX_PUSH_PER_ROUND:
            logger.warning(
                "本轮告警超过 %d 条（新 %d / 恢复 %d），其余留到下一轮",
                MAX_PUSH_PER_ROUND,
                len(new_keys),
                len(gone_keys),
            )

        return pushed_new, pushed_gone

    async def run(self) -> None:
        """后台循环。

        ⚠️ 每轮都 try/except：Prometheus 抖动、webhook 限流、Redis 重启都**不该让任务死掉**
            —— 死掉之后**没有人会再去拉起它**（它只是一个 asyncio task）。
        ⚠️ 配置是每轮现读的：改 `.env` 之后重启容器即可生效，不需要改代码。
        """
        interval = max(10, self.settings.alert_poll_interval_s)
        logger.info("告警搬运工已启动，每 %d 秒问一次 Prometheus", interval)
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("告警轮询失败（下一轮重试）：%s: %s", type(exc).__name__, exc)
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - started)))


def start_shipper(settings: Settings, redis: Redis) -> asyncio.Task[None] | None:
    """按配置决定要不要起搬运工。**没配 webhook 就返回 None**（不轮询、不报错）。"""
    if not settings.alert_webhook_url.strip():
        logger.info("告警未配置（ALERT_WEBHOOK_URL 为空）—— 跳过告警轮询，其余功能照旧")
        return None

    shipper = AlertShipper(settings, redis)
    return asyncio.create_task(_guarded(shipper), name="alert-shipper")


async def _guarded(shipper: AlertShipper) -> None:
    """把 `async with` 与循环包起来 —— 让 `start_shipper` 只返回一个裸 Task。"""
    async with shipper:
        await shipper.run()
