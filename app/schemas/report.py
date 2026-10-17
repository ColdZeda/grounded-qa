"""评测报告的出入参（**只读快照**）。

⚠️ 界面上的每个数字都必须能回答两件事：**什么时候跑的**、**当时是什么配置**。
    没有这两样的数字等于**裸数字** —— 换个 `fulltext_min` 它就能从 0.84 变 0.92，
    读者却以为那是"系统的检索准确率"。所以 `generated_at` 和 `config`
    是**必填字段**，不是可选的装饰。
"""

from __future__ import annotations

from pydantic import BaseModel


class ReportMetric(BaseModel):
    """一个指标。

    ⚠️ `value` 是**已经格式化好的字符串**（如 `0.920（23/25）`），不是裸 float：
        分子分母必须一起走。只给 `0.92` 的话，读者不知道是 25 题里的 23 题，
        还是 3 题里的 2.76 题 —— 这种数字在面试里经不起一句追问。
    """

    name: str
    value: str
    hint: str = ""


class ReportSnapshot(BaseModel):
    """一次评测运行的**聚合结论**（不含逐题明细）。"""

    #: 文件名（`retrieval-20261017T112745Z.json`），用来在界面上区分同一层的多次运行
    file: str
    kind: str  # retrieval / generation / refusal
    #: 原始时间戳，原样带出来（`20261017T112745Z`）
    generated_at: str
    #: 人能读的版本（`2026-10-17 11:27:45 UTC`）
    generated_at_display: str
    #: 这次跑了几道题 —— **所有比率的分母**
    questions: int
    metrics: list[ReportMetric]
    #: 那次运行的配置（检索参数 / 模型名 / 知识库）
    config: dict[str, str]


class ReportListOut(BaseModel):
    """报告页要的全部内容。"""

    snapshots: list[ReportSnapshot]
    #: 页面顶部那句必须说清的话（空目录 / 有文件读不了时尤其重要）
    note: str
