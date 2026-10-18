"""运维看板的数据源：替浏览器去问 Prometheus，把结果翻成**四组卡片**。

⚠️ **为什么由服务端代查，而不是让浏览器直连 Prometheus**：
    · 9090 上**没有鉴权**，公网暴露等于送人一个 PromQL 执行入口（也是一台 DoS 放大器）；
    · 浏览器要的是"每块卡片一个数"，不是 PromQL —— 让前端拼查询等于把
      "Prometheus 的 schema"和"页面"钉死在一起，改一条表达式要同时改两处。
    · **只允许查本文件里写好的表达式**（`PANELS`）。客户端传不了 query ⟹
      没有 SSRF、没有任意查询、也没有人能拿它扫 Prometheus 的整个时序空间。

⚠️ **Prometheus 不可达时返回 200 + `available=false`，不返回 5xx**：
    这是一个**读看板**的接口。它挂了的原因通常是"还没起 monitoring profile"或
    "容器刚重启" —— 那是**页面要显示的现状**，不是一次需要报警的接口故障。
    返回 502 会让前端只能整页报错，而正确的界面是"这一屏没数据 + 为什么"。

⚠️ 所有数字都带 `ts`（样本时间）和 `hint`（口径）。见 `app/schemas/monitoring.py`。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter

from app.api.deps import AdminUser
from app.core.config import get_settings
from app.core.limits import SHANGHAI
from app.schemas.monitoring import MonitorItem, MonitorPanel, MonitorSnapshot

logger = logging.getLogger("grounded.monitoring")

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

#: 单次查询超时。看板是**读**，慢就让它失败 —— 宁可显示"取不到"，也别让页面转 30 秒。
_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class _Panel:
    """一条卡片 = 一条 PromQL ＋ 它的口径说明。"""

    key: str
    group: str
    title: str
    unit: str
    hint: str
    expr: str


#: ⚠️ **改这里就要改卡片**：`key` 是前端认的稳定标识，`hint` 是"不许裸数字"的兑现。
#: ⚠️ 关于 SSE 的耗时口径：`/api/v1/chat` 的请求在**整条流结束**时才落指标 ⟹
#:    它的"耗时"是**用户看完全部回答**的时间，必须和普通请求分开看，
#:    混在一起会让 P95 看起来像"系统很慢"，其实那是流没关。
PANELS: tuple[_Panel, ...] = (
    # ---------- 可用性与延迟 ----------
    _Panel(
        key="api_up",
        group="可用性与延迟",
        title="API 抓取状态",
        unit="bool",
        hint="1 = Prometheus 能抓到 /metrics；0 = 抓不到（容器挂了或端口不对）",
        expr='up{job="grounded-api"}',
    ),
    _Panel(
        key="qps_5m",
        group="可用性与延迟",
        title="请求速率",
        unit="req_s",
        hint="近 5 分钟所有路径的平均每秒请求数（刚启动时会是 —，因为没有两个采样点）",
        expr="sum(rate(gq_http_requests_total[5m]))",
    ),
    _Panel(
        key="p95_plain_5m",
        group="可用性与延迟",
        title="普通请求 P95",
        unit="s",
        hint="近 5 分钟、排除问答流的 P95。分桶估算（桶边界见 app/core/metrics.py），不是精确分位数",
        expr=(
            "histogram_quantile(0.95, sum(rate(gq_http_request_duration_seconds_bucket"
            '{path!~"/api/v1/chat.*"}[5m])) by (le))'
        ),
    ),
    _Panel(
        key="p95_chat_5m",
        group="可用性与延迟",
        title="问答流 P95",
        unit="s",
        hint="近 5 分钟问答接口的 P95 = 用户从提问到回答流结束的时间，不是首字延迟",
        expr=(
            "histogram_quantile(0.95, sum(rate(gq_http_request_duration_seconds_bucket"
            '{path=~"/api/v1/chat.*"}[5m])) by (le))'
        ),
    ),
    _Panel(
        key="req_24h",
        group="可用性与延迟",
        title="24 小时请求数",
        unit="count",
        hint="近 24 小时累计（increase 会自动处理重启导致的计数器归零）",
        expr="sum(increase(gq_http_requests_total[24h]))",
    ),
    # ---------- 资源 ----------
    _Panel(
        key="cpu_5m",
        group="资源",
        title="CPU 使用率",
        unit="pct",
        hint="近 5 分钟主机平均（100 - 空闲占比）；2 核，50% 就已经很忙了",
        expr='100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    ),
    _Panel(
        key="mem_avail",
        group="资源",
        title="可用内存",
        unit="mb",
        hint="主机 MemAvailable（含可回收的缓存）；整机 2 GB，可用长期低于 200 MB 就该查了",
        expr="node_memory_MemAvailable_bytes / 1024 / 1024",
    ),
    _Panel(
        key="disk_avail",
        group="资源",
        title="根分区可用",
        unit="gb",
        hint="根分区的可用空间。40 GB 盘；低于 3 GB 时 docker pull / 备份会开始失败",
        expr='node_filesystem_avail_bytes{mountpoint="/"} / 1024 / 1024 / 1024',
    ),
    _Panel(
        key="load1",
        group="资源",
        title="1 分钟负载",
        unit="load",
        hint="主机 load1。2 核上持续 > 2 说明在排队；和 CPU 一起看才知道是算还是在等",
        expr="node_load1",
    ),
    # ---------- 问答与用量（24h）----------
    _Panel(
        key="chat_24h",
        group="问答与用量",
        title="24 小时问答结果",
        unit="count",
        hint="按结果分：answered 正常回答 / refused 拒答 / error 出错。拒答要单独看 —— 拒答率突然升高通常意味检索坏了，而不是用户问得怪",
        expr="sum(increase(gq_chat_outcome_total[24h])) by (outcome)",
    ),
    _Panel(
        key="refuse_rate_24h",
        group="问答与用量",
        title="24 小时拒答率",
        unit="ratio",
        hint="拒答次数 ÷ 全部问答次数。分母为 0 时显示 —（没有提问不等于 0% 拒答）",
        expr=(
            'sum(increase(gq_chat_outcome_total{outcome="refused"}[24h]))'
            " / clamp_min(sum(increase(gq_chat_outcome_total[24h])), 1e-9)"
        ),
    ),
    _Panel(
        key="tokens_24h",
        group="问答与用量",
        title="24 小时 token",
        unit="count",
        hint="模型 API 回报的 usage.total_tokens 累计（不是估算）。拒答不调模型 ⟹ 不计数",
        expr="sum(increase(gq_tokens_total[24h]))",
    ),
    # ---------- 限额与数据健康 ----------
    _Panel(
        key="limit_denied_24h",
        group="限额与数据健康",
        title="24 小时被限流",
        unit="count",
        hint="按 scope 分：rate = 次数超了（有人在刷）；quota = token 超了（有人在烧钱）",
        expr="sum(increase(gq_limit_denied_total[24h])) by (scope)",
    ),
    _Panel(
        key="limit_failopen_24h",
        group="限额与数据健康",
        title="24 小时计量失效",
        unit="count",
        hint="Redis 不可用而放行的次数。不为 0 就说明当前的限额数字不可信（这是静默放行，不报错）",
        expr="sum(increase(gq_limit_failopen_total[24h])) by (call)",
    ),
    _Panel(
        key="embedding_drift",
        group="限额与数据健康",
        title="向量模型不一致的块",
        unit="chunks",
        hint="库里 embedding_model 与当前配置不同的块数。恒为 0 才健康；非 0 说明换过模型但没重嵌入，那些块会被检索静默跳过",
        expr="gq_embedding_metadata_drift_chunks",
    ),
)


def _label_of(series: dict[str, Any]) -> str:
    """把 series 的标签拼成 `k=v,k=v`（丢掉 `__name__`，它是指标名不是标签）。"""
    metric = {k: v for k, v in (series.get("metric") or {}).items() if k != "__name__"}
    return ",".join(f"{key}={value}" for key, value in sorted(metric.items()))


async def _query(client: httpx.AsyncClient, base: str, expr: str) -> list[MonitorItem]:
    """查一条表达式。**只有 HTTP / 解析失败会抛**；"没数据"返回空列表。"""
    response = await client.get(f"{base}/api/v1/query", params={"query": expr})
    response.raise_for_status()
    payload = response.json()
    data = payload["data"]
    result = data["result"]

    # 标量（不带标签的常数表达式会走这里）
    if data.get("resultType") == "scalar":
        ts, value = result
        return [MonitorItem(value=float(value), ts=float(ts))]

    items: list[MonitorItem] = []
    for series in result:
        ts, value = series["value"]
        items.append(MonitorItem(label=_label_of(series), value=float(value), ts=float(ts)))
    return items


async def collect(
    base_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[MonitorPanel]:
    """并发查全部表达式 → 卡片列表。

    ⚠️ **一条失败就整体失败**（不返回半份数据）：半份看板比没有看板更危险 ——
        它会让人以为"其他指标都正常"。所以 `gather` 出去，有异常就往上抛。

    📌 `transport` 只为测试而存在（塞一个假 HTTP 层就能验解析逻辑，不连网络）；
        生产调用不传它，走默认的 httpx 传输。
    """
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
        results = await asyncio.gather(*(_query(client, base, panel.expr) for panel in PANELS))

    return [
        MonitorPanel(
            key=panel.key,
            group=panel.group,
            title=panel.title,
            unit=panel.unit,
            hint=panel.hint,
            items=items,
        )
        for panel, items in zip(PANELS, results, strict=True)
    ]


@router.get("/snapshot", response_model=MonitorSnapshot, summary="运维看板快照（读 Prometheus）")
async def snapshot(_admin: AdminUser) -> MonitorSnapshot:
    """一屏运维数据。

    ⚠️ **要管理员**：这是运维面（能看出流量、成本、错误分布），不是业务数据。
        普通成员看业务报告就够，看这些只会多一份"能被拿来推断内部状态"的入口。
    ⚠️ 时间用**北京时间**：所有"今天 / 24 小时"的口径都按它切（见 `app/core/limits.py`）。
    """
    settings = get_settings()
    now = datetime.now(SHANGHAI)
    base = (settings.prometheus_url or "").strip()
    stale_after_s = 3 * settings.prometheus_scrape_interval_s

    def _snapshot(**kwargs: Any) -> MonitorSnapshot:
        return MonitorSnapshot(
            generated_at_display=now.strftime("%Y-%m-%d %H:%M:%S"),
            prometheus_url=base,
            stale_after_s=stale_after_s,
            **kwargs,
        )

    if not base:
        return _snapshot(
            available=False,
            error="没配 PROMETHEUS_URL —— 看板拿不到数据（设成 http://prometheus:9090 即可）",
        )

    try:
        panels = await collect(base)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Prometheus 查询失败（%s）：%s", base, exc)
        return _snapshot(
            available=False,
            error=f"连不上 {base}（{type(exc).__name__}）。多半是没起 monitoring profile。",
        )

    return _snapshot(available=True, panels=panels)
