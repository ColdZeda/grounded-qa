"""阶段 7 配对评测：**同一批题，pipeline vs agent，逐题对照**（`PLAN §13.4`）。

## 为什么必须配对

非确定性与"两条路实现不同"会同时污染结论。配对（同一题、同一模型、同一判分器、
同一批知识库）之后，**差值**才是 agent 的净效果；否则"agent 分低"可能只是
"这次跑的时候检索恰好差一点"。

## 验收标准（写死，防止无尽调优 · 2026-10-26 按 W19 结论改过一次）

1. **主指标 · 澄清组**（`insufficient`）：反问命中 **≥ pipeline +20 pt**
   —— pipeline 结构上做不到反问，这一条量的是 agent 独有的能力。
2. **护栏 · 多跳 / 多诉求组**：不劣于 **−1 题**（各 6 / 5 题，题干都是要点命中）
3. **单跳对照组**：不劣于 **−1 题**
4. 平均步数 **≤ 2.5**，p50 / p95 延迟与 token 有数
5. 关掉开关（`open_agent_mode=false` 或用户选「快速」）时行为与加入 agent 之前逐字节一致
   → 由单测守着，不在这里跑

⚠️ **为什么①从"多跳/澄清组"改成"澄清组"**：原先把两组混成一个指标，而这两组要求
**相反的行为**（多跳要推断、信息不足要反问），agent 只能选一边 —— 混着算等于用一半题
去惩罚另一半。取舍与三轮实测数字 → `CANDIDATES.md` W19 / `docs/architecture.md` §9.51。

## 怎么跑

    uv run python evals/agent_paired.py                 # 全跑（22 题 × 2 条路，要花 token）
    uv run python evals/agent_paired.py --repeat 3      # ⚠️ **报数就该用这个**：单跑一次不可信
    uv run python evals/agent_paired.py --limit 3       # 先试水
    uv run python evals/agent_paired.py --kind insufficient
    uv run python evals/agent_paired.py --arm agent     # 只跑一路（省一半钱）

⚠️ **这会真的调模型**（不是单测），花的是真钱。默认**不跑**，只能手动执行。
⚠️ 结果同时落 `evals/results/agent-paired-<时间>.json` —— 数字要能复查，不能只留在终端里。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.tenant import Tenant
from app.db.session import dispose_engine, get_session_factory
from app.rag.agent import AgentRunner
from app.rag.pipeline import RagPipeline, build_clients
from evals.run import _norm, _point_hit, _strip_citation_marks

QUESTION_FILE = Path(__file__).with_name("questions") / "agent.yaml"
RESULT_DIR = Path(__file__).with_name("results")

KINDS = ("multi_hop", "multi_intent", "insufficient", "single_hop")


@dataclass
class ArmResult:
    """一条路上的一次作答。"""

    text: str = ""
    refused: bool = False
    latency_ms: int = 0
    steps: int = 0
    retries: int = 0
    tokens: int = 0
    tools: list[str] = field(default_factory=list)
    degraded: str | None = None
    error: str | None = None


@dataclass
class Row:
    """一题两路的结果。"""

    id: str
    kind: str
    question: str
    pipeline: ArmResult = field(default_factory=ArmResult)
    agent: ArmResult = field(default_factory=ArmResult)
    #: **每次重复各记一条**（`--repeat N`）。
    #: ⚠️ 2026-10-26 实测：同一套题跑两次，个别题会翻（C02 ✗→✓、A04 ✓→✗）
    #:    —— **单跑一次的差值不能当结论**。
    pipeline_hits: list[bool] = field(default_factory=list)
    agent_hits: list[bool] = field(default_factory=list)
    tools_ok: bool | None = None
    #: 这道题**至少**该检索几次（多跳组的"多步行为"证据，不参与判分）。
    min_search: int = 0

    @property
    def hit_pipeline(self) -> bool:
        """多次重复时取**多数** —— 一道题的结论不该被一次偶然带偏。"""
        return _majority(self.pipeline_hits)

    @property
    def hit_agent(self) -> bool:
        return _majority(self.agent_hits)


def _load_questions() -> list[dict[str, Any]]:
    with QUESTION_FILE.open(encoding="utf-8") as handle:
        return list(yaml.safe_load(handle))


async def _tenant_id(session: AsyncSession, slug: str) -> uuid.UUID:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"库里没有 slug={slug!r} 的租户 —— 先跑 `uv run python -m app.db.seed`")
    return tenant.id


def _hit(item: dict[str, Any], answer: ArmResult) -> bool:
    """这一题算不算答对。

    ⚠️ 两类题的判据**完全不同**，不能混成一条：
        · 有 `required_points` → 要点全部命中（复用 `_point_hit`）；
        · `expect_clarify` → **反问就算对**（答案里有疑问标记，或根本没给结论）。
          这一类 pipeline 结构上做不到，所以它正是 agent 该赢的地方。
    """
    if item.get("expect_clarify"):
        if answer.refused:
            return False
        text = answer.text.strip()
        return "？" in text or "?" in text
    points = item.get("required_points") or []
    if not points:
        return False
    norm = _norm(_strip_citation_marks(answer.text))
    return all(_point_hit(point, norm)[0] for point in points)


def _majority(values: list[bool]) -> bool:
    """多数决（空则 False）。"""
    return bool(values) and sum(values) * 2 > len(values)


def _rate(values: list[bool]) -> float:
    """命中率（空则 0）。"""
    return sum(values) / len(values) if values else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _quantile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(int(len(ordered) * q), len(ordered) - 1)
    return ordered[index]


async def _run_pipeline(
    session: AsyncSession, *, tenant_id: uuid.UUID, question: str, clients: Any
) -> ArmResult:
    chat, embedder, reranker = clients
    pipeline = RagPipeline(
        session=session, tenant_id=tenant_id, chat=chat, embedder=embedder, reranker=reranker
    )
    started = time.perf_counter()
    result = ArmResult()
    try:
        async for event in pipeline.answer(question=question):
            if event.__class__.__name__ == "Answer":
                result.text = event.text
                result.refused = event.refused
                result.tokens = int(event.usage.get("total_tokens") or 0)
                result.error = event.error
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


async def _run_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, question: str, clients: Any
) -> ArmResult:
    chat, embedder, reranker = clients
    settings = get_settings()
    runner = AgentRunner(
        session=session,
        tenant_id=tenant_id,
        chat=chat,
        question=question,
        fallback=lambda: RagPipeline(
            session=session, tenant_id=tenant_id, chat=chat, embedder=embedder, reranker=reranker
        ),
        embedder=embedder,
        reranker=reranker,
        settings=settings,
    )
    started = time.perf_counter()
    result = ArmResult()
    try:
        async for event in runner.answer():
            name = event.__class__.__name__
            if name == "ToolEvent":
                result.tools.append(event.name)
            elif name == "Answer":
                result.text = event.text
                result.refused = event.refused
                result.tokens = int(event.usage.get("total_tokens") or 0)
                result.steps = int(event.extra.get("agent_steps") or 0)
                result.retries = int(event.extra.get("agent_retries") or 0)
                result.degraded = event.extra.get("degraded")  # type: ignore[assignment]
                result.error = event.error
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


def _tools_match(expected: list[str], got: list[str]) -> bool | None:
    """工具选择对不对（**只看"该调的调了没"**，不要求顺序完全一致）。"""
    if not expected:
        return None
    return all(name in got for name in expected)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    questions = _load_questions()
    if args.kind:
        questions = [item for item in questions if item["kind"] == args.kind]
    if args.limit:
        questions = questions[: args.limit]

    clients = build_clients(settings)
    factory = get_session_factory()
    rows: list[Row] = []

    try:
        async with factory() as session:
            tenant_id = await _tenant_id(session, args.tenant)
            for item in questions:
                row = Row(
                    id=item["id"],
                    kind=item["kind"],
                    question=item["question"],
                    min_search=int(item.get("min_search") or 0),
                )
                for round_index in range(max(1, args.repeat)):
                    if args.arm in ("both", "pipeline"):
                        result = await _run_pipeline(
                            session, tenant_id=tenant_id, question=item["question"], clients=clients
                        )
                        row.pipeline_hits.append(_hit(item, result))
                        if round_index == 0:
                            row.pipeline = result
                    if args.arm in ("both", "agent"):
                        result = await _run_agent(
                            session, tenant_id=tenant_id, question=item["question"], clients=clients
                        )
                        row.agent_hits.append(_hit(item, result))
                        if round_index == 0:
                            row.agent = result
                            row.tools_ok = _tools_match(
                                item.get("expect_tools") or [], result.tools
                            )
                rows.append(row)
                spread_p = "".join("✓" if hit else "✗" for hit in row.pipeline_hits) or "-"
                spread_a = "".join("✓" if hit else "✗" for hit in row.agent_hits) or "-"
                print(
                    f"{row.id:5s} {row.kind:12s} pipeline={spread_p} agent={spread_a} "
                    f"steps={row.agent.steps} tools={','.join(row.agent.tools) or '-'}"
                )
    finally:
        await dispose_engine()

    _report(rows, arm=args.arm)
    if args.dump:
        RESULT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = RESULT_DIR / f"agent-paired-{stamp}.json"
        payload = {
            "ran_at": datetime.now(UTC).isoformat(),
            "llm_model": settings.llm_model,
            "rerank_model": settings.rerank_model,
            "embedding_model": settings.embedding_model,
            "answer_mode_default": settings.open_agent_mode,
            "rows": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "question": row.question,
                    "hit_pipeline": row.hit_pipeline,
                    "hit_agent": row.hit_agent,
                    # ⚠️ **逐轮原始结果也落盘**：只存多数结论的话，
                    #    “这次到底抖不抖”就又只能靠回忆了。
                    "pipeline_hits": row.pipeline_hits,
                    "agent_hits": row.agent_hits,
                    "tools_ok": row.tools_ok,
                    "min_search": row.min_search,
                    "search_count": row.agent.tools.count("search_kb"),
                    "pipeline": _as_dict(row.pipeline),
                    "agent": _as_dict(row.agent),
                }
                for row in rows
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已落盘：{path}")
    return 0


def _as_dict(result: ArmResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "refused": result.refused,
        "latency_ms": result.latency_ms,
        "steps": result.steps,
        "retries": result.retries,
        "tokens": result.tokens,
        "tools": result.tools,
        "degraded": result.degraded,
        "error": result.error,
    }


def _report(rows: list[Row], *, arm: str) -> None:
    print("\n" + "=" * 78)
    print("按题型汇总（要点命中率）")
    print("=" * 78)
    for kind in KINDS:
        group = [row for row in rows if row.kind == kind]
        if not group:
            continue
        total = len(group)
        rate_p = sum(_rate(row.pipeline_hits) for row in group) / total
        rate_a = sum(_rate(row.agent_hits) for row in group) / total
        spread = ""
        rounds = max((len(row.agent_hits) for row in group), default=0)
        if rounds > 1:
            per_round = [
                sum(row.agent_hits[index] for row in group if len(row.agent_hits) > index) / total
                for index in range(rounds)
            ]
            spread = f"  agent 逐轮 {min(per_round):.0%}~{max(per_round):.0%}"
        print(
            f"{kind:12s} n={total:2d}  pipeline {_pct(rate_p)}  agent {_pct(rate_a)}  "
            f"Δ {(rate_a - rate_p) * 100:+5.1f} pt{spread}"
        )

    if arm == "both":
        print("\n" + "=" * 78)
        print("验收标准（PLAN §13.4）")
        print("=" * 78)
        # ① 主指标：澄清组（pipeline 结构上做不到"反问"）
        clarify = [row for row in rows if row.kind == "insufficient"]
        if clarify:
            hp = sum(row.hit_pipeline for row in clarify)
            ha = sum(row.hit_agent for row in clarify)
            delta = (ha - hp) / len(clarify) * 100
            verdict = "✅ 达标" if delta >= 20 else "❌ 未达标"
            print(
                f"① 主指标 · 澄清组 ≥ +20 pt：Δ {delta:+.1f} pt"
                f"（agent {ha}/{len(clarify)} vs pipeline {hp}/{len(clarify)}）{verdict}"
            )
        # ② 护栏：多跳 / 多诉求组（要点命中，答错就是答错，不许比 pipeline 更差）
        depth = [row for row in rows if row.kind in ("multi_hop", "multi_intent")]
        if depth:
            hp = sum(row.hit_pipeline for row in depth)
            ha = sum(row.hit_agent for row in depth)
            verdict = "✅ 达标" if ha >= hp - 1 else "❌ 退化过多"
            print(
                f"② 护栏 · 多跳/多诉求不劣于 −1 题：agent {ha}/{len(depth)} vs pipeline "
                f"{hp}/{len(depth)}（{ha - hp:+d} 题）{verdict}"
            )
        # ③ 单跳对照组
        single = [row for row in rows if row.kind == "single_hop"]
        if single:
            hp = sum(row.hit_pipeline for row in single)
            ha = sum(row.hit_agent for row in single)
            verdict = "✅ 达标" if ha >= hp - 1 else "❌ 退化过多"
            print(
                f"③ 单跳对照组不劣于 −1 题：agent {ha}/{len(single)} vs pipeline {hp}/{len(single)} {verdict}"
            )

        steps = [row.agent.steps for row in rows if row.agent.steps]
        if steps:
            avg = statistics.fmean(steps)
            verdict = "✅ 达标" if avg <= 2.5 else "❌ 步数偏多"
            print(f"④ 平均步数 ≤ 2.5：{avg:.2f} {verdict}")
        retries = [row.agent.retries for row in rows if row.agent.steps]
        if retries:
            print(f"   自愈重试率：{sum(1 for item in retries if item) / len(retries) * 100:.1f}%")

        # 多步检索的证据：多跳题要求它真的检索了 ≥2 次（不是一步蒙对）
        multi = [item for item in rows if item.kind == "multi_hop"]
        if multi:
            deep = sum(
                1 for row in multi if row.agent.tools.count("search_kb") >= (row.min_search or 2)
            )
            counts = ",".join(f"{row.id}:{row.agent.tools.count('search_kb')}" for row in multi)
            print(f"   多跳组真的检索 ≥2 次的：{deep}/{len(multi)}（{counts}）")

        lat_p = [row.pipeline.latency_ms for row in rows if row.pipeline.latency_ms]
        lat_a = [row.agent.latency_ms for row in rows if row.agent.latency_ms]
        if lat_p and lat_a:
            print(
                f"   p50 延迟：pipeline {_quantile(lat_p, 0.5)} ms / agent {_quantile(lat_a, 0.5)} ms；"
                f"p95：{_quantile(lat_p, 0.95)} / {_quantile(lat_a, 0.95)} ms"
            )
        tok_p = sum(row.pipeline.tokens for row in rows)
        tok_a = sum(row.agent.tokens for row in rows)
        print(
            f"   token 合计：pipeline {tok_p} / agent {tok_a}（×{tok_a / tok_p:.2f}）"
            if tok_p
            else ""
        )

        degraded = [row for row in rows if row.agent.degraded]
        if degraded:
            print(
                f"   回落 pipeline 的题：{len(degraded)} 条（{', '.join(row.id for row in degraded)}）"
            )
        graded = [row for row in rows if row.tools_ok is not None]
        if graded:
            ok = sum(1 for row in graded if row.tools_ok)
            print(f"   工具选择准确率：{ok}/{len(graded)}（人工金标准轨迹）")

    errors = [row for row in rows if row.pipeline.error or row.agent.error]
    if errors:
        print("\n出错的题：")
        for row in errors:
            print(f"  {row.id}: pipeline={row.pipeline.error} agent={row.agent.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="agent 配对评测（pipeline vs agent）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每道题跑几次（模型非确定性 ⇒ 建议 3）",
    )
    parser.add_argument("--kind", choices=KINDS, help="只跑某一题型")
    parser.add_argument("--arm", choices=("both", "pipeline", "agent"), default="both")
    parser.add_argument("--tenant", default="default", help="租户 slug（默认 default）")
    parser.add_argument("--no-dump", dest="dump", action="store_false", help="不落 JSON")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
