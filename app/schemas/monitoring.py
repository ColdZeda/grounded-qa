"""运维看板的返回结构。

⚠️ **不许裸数字**（本项目的硬规矩，见 `app/schemas/report.py`）：所以每个数字都带着
    · `ts` —— 那个样本是什么时候采的（Prometheus 给的，不是我们现编的）
    · 所在 panel 的 `hint` —— 口径：窗口多长、是分桶估算还是精确值、分母是什么
    · 整个快照的 `generated_at_display` —— 这一屏是什么时候问的
    缺数据时 `value=None`，前端显示 `—`，**不显示 0**（0 和"没有"是两件事）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MonitorItem(BaseModel):
    """一条时间序列在当前时刻的取值。"""

    #: 序列的标签（如 `outcome=answered`）。单值序列为空串。
    label: str = ""
    #: 取值。`None` = 没有采到数据（**不是零**）。
    value: float | None = None
    #: 样本时间（epoch 秒）。`None` = 没有样本。
    ts: float | None = None


class MonitorPanel(BaseModel):
    """一块卡片 = 一条（或一组）PromQL 的结果。"""

    key: str
    #: 卡片归到哪一组（前端按它分栏）
    group: str
    title: str
    #: 单位。前端据此决定小数位与后缀：`ratio` / `pct` / `s` / `req_s` / `mb` / `gb` / `count` / `bool`
    unit: str
    #: 口径说明 —— **必须写**，否则就是裸数字
    hint: str
    items: list[MonitorItem] = Field(default_factory=list)


class MonitorSnapshot(BaseModel):
    available: bool
    generated_at_display: str
    #: 查询的是哪个 Prometheus（写出来，避免"以为在看生产"其实是本地栈）
    prometheus_url: str
    #: 样本比这个秒数还旧就标黄。**阈值是判断，不是从 Prometheus 读的**（3 个抓取周期）
    stale_after_s: int = 90
    panels: list[MonitorPanel] = Field(default_factory=list)
    #: Prometheus 不可达时的原因（`available=False` 时才有）
    error: str | None = None
