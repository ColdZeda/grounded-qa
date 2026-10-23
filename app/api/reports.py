"""只读评测报告：把 `evals/results/*.json` 读成**能讲清楚的数字**。

⚠️ **这个接口不在服务器上跑评测**（那是 `evals/run.py`，只在开发机上跑）。
    它只是把已经跑出来的结果读出来展示 —— 这就是"报告数据上线、评分材料不上线"的
    具体落地：`evals/results/*.json` 进镜像，`evals/questions/` 与 `run.py` 不进。

⚠️ **只返回聚合，不返回逐题明细。**
    `results/*.json` 里带着题干、标准答案、`must_hit` 文件名 —— 那是**评分材料**。
    把它们通过 HTTP 吐出去，等于把题集公开了；而这个页面要的只是"几道题里对了几道"。
    结论：**数字上线，逐题明细不上线**（两者在同一个文件里，所以在接口这一层切开）。

⚠️ **不许裸数字**（见 `app/schemas/report.py` 的模块说明）：
    每条指标都要带分母，每份快照都要带**生成时间 ＋ 那次的配置**。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.core.config import get_settings
from app.schemas.report import ReportListOut, ReportMetric, ReportSnapshot

logger = logging.getLogger("grounded.reports")

router = APIRouter(prefix="/reports", tags=["reports"])

#: 仓库根（容器里是 `/app`）。`app/api/reports.py` 往上三层。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: `20261017T112745Z` → `2026-10-17 11:27:45 UTC`
_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")


def results_dir() -> Path:
    """结果目录。相对路径按**仓库根**解析，不受进程 cwd 影响。"""
    configured = Path(get_settings().evals_results_dir)
    return configured if configured.is_absolute() else REPO_ROOT / configured


def _display_time(stamp: str) -> str:
    match = _STAMP_RE.match(stamp)
    if match is None:
        # 认不出来就原样带出去 —— **不要猜**，猜出来的时间比没有时间更糟
        return stamp
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}:{second} UTC"


def _ratio(value: Any, total: int) -> str:
    """`x/total`。

    ⚠️ 分母为零时给 `—` 而不是 `0/0` 或 `0.000`。
        空题的比率是**不存在**，不是零 —— 写成 0.000 会被当成"全错"。
    """
    if total <= 0:
        return "—"
    return f"{value}/{total}"


def _num(value: Any) -> str:
    """浮点保留三位。`None` → `—`（同上：缺值不等于零）。"""
    if isinstance(value, (int, float)):
        return f"{float(value):.3f}"
    return "—"


def _summarize_retrieval(payload: dict[str, Any], *, filename: str, stamp: str) -> ReportSnapshot:
    """检索层：一份文件里可能有**多组消融**（全文 / 向量 / RRF / RRF+精排）。"""
    top_k = payload.get("top_k")
    meta = payload.get("meta") or {}
    questions = int(meta.get("questions") or 0)
    ablations = payload.get("ablations") or []

    k_label = f"@{top_k}" if isinstance(top_k, int) else ""
    metrics: list[ReportMetric] = []
    for ablation in ablations:
        name = str(ablation.get("name") or "?")
        metrics.append(
            ReportMetric(
                name=f"{name} · 命中率{k_label}",
                value=f"{_num(ablation.get('hit_at_k'))}（{_ratio(ablation.get('hit_count'), questions)}）",
                hint="前 k 条里出现了该题的标准出处",
            )
        )
        metrics.append(
            ReportMetric(
                name=f"{name} · MRR@10",
                value=_num(ablation.get("mrr_at_10")),
                hint="标准出处排名倒数的均值；1.0 = 永远排第一",
            )
        )

    config: dict[str, str] = {
        "知识库": str(meta.get("kb") or "—"),
        "消融组": " / ".join(str(item.get("name")) for item in ablations) or "—",
    }
    raw_config = payload.get("config") or {}
    for key in (
        "retrieve_top_k",
        "rrf_k",
        "fulltext_min_similarity",
        "embedding_model",
        "rerank_model",
    ):
        if key in raw_config:
            config[key] = str(raw_config[key])
    if isinstance(top_k, int):
        config["top_k"] = str(top_k)

    return ReportSnapshot(
        file=filename,
        kind="retrieval",
        generated_at=stamp,
        generated_at_display=_display_time(stamp),
        questions=questions,
        metrics=metrics,
        config=config,
    )


def _summarize_answer_layer(
    payload: dict[str, Any], *, filename: str, stamp: str, kind: str
) -> ReportSnapshot:
    """生成层 / 拒答层：逐题记录 → 几个比率。"""
    results = payload.get("results") or []
    total = len(results)

    passed = sum(1 for item in results if item.get("passed"))
    refused = sum(1 for item in results if item.get("refused"))
    zero_calls = sum(1 for item in results if not item.get("llm_calls"))
    forbidden = sum(1 for item in results if item.get("forbidden_hit"))
    cite_checked = [item for item in results if item.get("cite_ok") is not None]
    cite_ok = sum(1 for item in cite_checked if item.get("cite_ok"))
    must_say_checked = [item for item in results if item.get("must_say_ok") is not None]
    must_say_ok = sum(1 for item in must_say_checked if item.get("must_say_ok"))

    if kind == "refusal":
        metrics = [
            ReportMetric(
                name="行为正确",
                value=_ratio(passed, total),
                hint="该拒的拒了、该答的答了，且内容要点齐全",
            ),
            ReportMetric(
                name="真的拒答了",
                value=_ratio(refused, total),
                hint="其中「库里没有但应当给出说明」的题，正确行为是**不拒答**",
            ),
            ReportMetric(
                name="一次模型都没调",
                value=_ratio(zero_calls, total),
                hint="拒答是调用前的确定性判断（不花钱、不受模型心情影响）",
            ),
            ReportMetric(
                name="内容要点达标",
                value=_ratio(must_say_ok, len(must_say_checked)),
                hint="对「不该拒答」的题，检查必须说到的结论",
            ),
        ]
    else:
        metrics = [
            ReportMetric(
                name="全通过",
                value=_ratio(passed, total),
                hint="要点全中 ＋ 引用对 ＋ 无禁语",
            ),
            ReportMetric(
                name="引用正确",
                value=_ratio(cite_ok, len(cite_checked)),
                hint="回答里的 [n] 是否指向真正支撑它的那段",
            ),
            ReportMetric(
                name="出现禁语",
                value=_ratio(forbidden, total),
                hint="越小越好：把已废止或未生效的说法当现行政策说出来",
            ),
        ]

    config = {
        "知识库": str(payload.get("kb") or "—"),
        "对话模型": str(payload.get("llm_model") or "—"),
    }
    return ReportSnapshot(
        file=filename,
        kind=kind,
        generated_at=stamp,
        generated_at_display=_display_time(stamp),
        questions=total,
        metrics=metrics,
        config=config,
    )


def _load(path: Path) -> ReportSnapshot | None:
    """读一个结果文件。读不懂就跳过（并记日志）—— 一个坏文件不该让整页 500。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("评测结果读不了，已跳过：%s（%s）", path.name, exc)
        return None

    if not isinstance(payload, dict):
        logger.warning("评测结果不是对象，已跳过：%s", path.name)
        return None

    kind = str(payload.get("kind") or "")
    stamp = str(payload.get("generated_at") or "")
    if not stamp:
        logger.warning("评测结果没有 generated_at，已跳过：%s", path.name)
        return None

    if kind == "retrieval":
        return _summarize_retrieval(payload, filename=path.name, stamp=stamp)
    if kind in {"generation", "refusal"}:
        return _summarize_answer_layer(payload, filename=path.name, stamp=stamp, kind=kind)

    logger.warning("不认识的评测结果类型，已跳过：%s（kind=%r）", path.name, kind)
    return None


@router.get("", response_model=ReportListOut, summary="评测报告快照（只读）")
async def list_reports(current_user: CurrentUser) -> ReportListOut:
    """列出所有评测快照，**新的在前**。

    ⚠️ 需要登录才能看（不是公开接口）：这些是项目的过程数据，
        而且里面有"禁语清单"这类自曝弱点的东西。
    ⚠️ **不在请求里跑评测** —— 只读已经存在的文件。
    """
    _ = current_user  # 只为鉴权；报告是全局的，不按租户分
    directory = results_dir()
    if not directory.is_dir():
        return ReportListOut(
            snapshots=[],
            note=f"没找到评测结果目录：{directory}。先在开发机上跑 evals/run.py 再把结果放进仓库。",
        )

    snapshots: list[ReportSnapshot] = []
    for path in sorted(directory.glob("*.json")):
        snapshot = _load(path)
        if snapshot is not None:
            snapshots.append(snapshot)

    # 时间戳格式固定（`YYYYMMDDTHHMMSSZ`），所以字符串倒序 = 时间倒序
    snapshots.sort(key=lambda item: item.generated_at, reverse=True)

    note = (
        f"共 {len(snapshots)} 份快照，新的在前。「这是只读快照，不是在页面上现跑的评测」；"
        "同一个指标出现多个数值，是因为判分口径随时间改过 —— 比较时请认准同一份快照。"
    )
    return ReportListOut(snapshots=snapshots, note=note)


@router.get("/conflicts", summary="知识库冲突检查（按需现算）")
async def knowledge_conflicts(session: SessionDep, current_admin: AdminUser) -> dict[str, Any]:
    """同一主题、同一适用范围，两份文档写着不同的数字 —— 列出来给人看。

    ⚠️ **和上面的评测报告不是一回事**：那是"跑分快照"（只读文件），这是**现算的体检**。
        放在同一个路由前缀下，是因为前端将来都在"报告"页读它；**页面接入还没做**，
        现在它是给人和脚本用的 JSON。
    ⚠️ **只管理员能看**：里面会把**内部材料**和**现行政策**并排摆出来，
        普通成员看见"内部话术库和官网不一致"没有任何意义，反而容易拿去对客户讲。
    ⚠️ 判定逻辑与取舍（为什么只比同 scope、为什么跳过讲解稿）→ `app/kb/conflicts.py`。
    """
    from app.kb.conflicts import build_report, collect

    report = await collect(session, tenant_id=current_admin.tenant_id)
    return build_report(report)
