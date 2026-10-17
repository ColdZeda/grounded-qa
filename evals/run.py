"""检索层评测 —— 四组消融。

**只跑检索，不调 LLM。** 花的钱只有 embedding（每个问句一次）＋ rerank（每题一次）。

为什么要有这个脚本（而不是"感觉检索还行"）：

    一个错答可能出在三处 —— 没召回（检索层）／召回了但没用对（生成层）／
    该说没有却编了（拒答层）。混在一起打一个总分，**改不动**。
    所以先只测检索层：给定问句，目标文档有没有进 top-k。

四组消融（同一批题、同一个库、只换检索策略）：

    fulltext  只用 pg_trgm 的 word_similarity（单路）
    vector    只用 pgvector 余弦（单路）
    rrf       两路 + 倒数排名融合（线上默认的前半步）
    rerank    rrf 之后再过一次精排模型（线上默认的完整前半程）

判分口径：
    hit@k   目标文档（`must_hit`）里**任意一个**出现在 top-k 的文档去重排序里
    MRR@10  第一个命中的目标文档的位次的倒数，未命中记 0

⚠️ 排序是 **chunk 级**，判分是 **文档级** —— 同一文档的多块只取最靠前的那块。
   这与"引用来源"的呈现方式一致（线上引用是按文档去重的）。

用法：

    uv run python evals/run.py --layer retrieval                 # 四组全跑
    uv run python evals/run.py --layer retrieval --only rrf       # 只跑一组
    uv run python evals/run.py --layer retrieval --limit 3        # 先跑 3 题试水
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models.knowledge import Document
from app.db.models.tenant import Tenant
from app.db.session import dispose_engine, get_session_factory
from app.rag.pipeline import build_clients
from app.rag.providers import ModelAPIError, aclose_http_client
from app.rag.retrieval import HybridRetriever, RetrievedChunk

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_DIR = REPO_ROOT / "evals" / "questions"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

ABLATIONS = ("fulltext", "vector", "rrf", "rerank")

#: 问句 -> 向量。跨消融复用，保证四组看到的是同一个向量。
_EMBED_CACHE: dict[str, list[float]] = {}


# ---------------------------------------------------------------- 数据加载


@dataclass(frozen=True)
class RetrievalQuestion:
    """`retrieval.yaml` 里的一条。"""

    id: str
    question: str
    must_hit: list[str]
    why_not_obvious: str = ""


def load_retrieval_questions(path: Path) -> list[RetrievalQuestion]:
    """读题集。文件里的注释行由 yaml 自己丢掉。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{path} 的顶层结构不是列表，读到的类型是 {type(raw).__name__}")
    return [
        RetrievalQuestion(
            id=item["id"],
            question=item["question"],
            must_hit=list(item["must_hit"]),
            why_not_obvious=item.get("why_not_obvious", ""),
        )
        for item in raw
    ]


class DocumentPaths:
    """`document_id -> 相对路径`，用来把 chunk 级命中折算成文档级。

    ⚠️ 入库时 `filename` 存的是 `<kb>/<相对路径>`（如 `senyu/10-退换货/11-地区差异.md`），
       而题集里的 `must_hit` 写的是**相对语料根**的路径。这里统一去掉 `<kb>/` 前缀，
       否则每题都会判成"没命中" —— 而这种错会**静默**地把分数打成 0。
    """

    def __init__(self, mapping: dict[uuid.UUID, str], *, kb: str) -> None:
        self._mapping = mapping
        self._prefix = f"{kb}/"

    def path_of(self, document_id: uuid.UUID) -> str:
        filename = self._mapping.get(document_id, "")
        if filename.startswith(self._prefix):
            return filename[len(self._prefix) :]
        return filename

    @property
    def document_count(self) -> int:
        return len(self._mapping)

    def doc_ranking(self, hits: Sequence[RetrievedChunk], *, limit: int) -> list[str]:
        """把 chunk 排序折算成**文档去重排序**（首个出现的位置即该文档的名次）。"""
        seen: list[str] = []
        for hit in hits:
            path = self.path_of(hit.document_id)
            if path and path not in seen:
                seen.append(path)
                if len(seen) >= limit:
                    break
        return seen


# ---------------------------------------------------------------- 判分


@dataclass
class QuestionResult:
    id: str
    question: str
    must_hit: list[str]
    rank: int | None = None  # 第一个命中的目标文档的位次（1 起）；None = 未命中
    top_docs: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class AblationReport:
    name: str
    results: list[QuestionResult]

    @property
    def hit_count(self) -> int:
        return sum(1 for r in self.results if r.rank is not None)

    def hit_at(self, k: int) -> float:
        scored = [r for r in self.results if r.error is None]
        if not scored:
            return 0.0
        return sum(1 for r in scored if r.rank is not None and r.rank <= k) / len(scored)

    def mrr_at(self, k: int) -> float:
        scored = [r for r in self.results if r.error is None]
        if not scored:
            return 0.0
        total = sum(1.0 / r.rank for r in scored if r.rank is not None and r.rank <= k)
        return total / len(scored)

    def mean_rank(self) -> float | None:
        ranks = [r.rank for r in self.results if r.rank is not None]
        return statistics.fmean(ranks) if ranks else None

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.error is not None)


def _rank_of(doc_ranking: Sequence[str], must_hit: Sequence[str]) -> int | None:
    """在文档排序里找第一个属于 `must_hit` 的文档，返回其位次（1 起）。"""
    wanted = set(must_hit)
    for index, path in enumerate(doc_ranking, start=1):
        if path in wanted:
            return index
    return None


# ---------------------------------------------------------------- 主流程


async def _resolve_tenant_id(session: Any, slug: str) -> uuid.UUID:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"库里没有 slug={slug!r} 的租户 —— 先跑 `python -m app.db.seed`")
    return tenant.id


async def _document_paths(session: Any, tenant_id: uuid.UUID, *, kb: str) -> DocumentPaths:
    rows = (
        await session.execute(
            select(Document.id, Document.filename).where(Document.tenant_id == tenant_id)
        )
    ).all()
    mapping = {doc_id: filename for doc_id, filename in rows if filename.startswith(f"{kb}/")}
    if not mapping:
        raise SystemExit(f"库里没有 {kb!r} 这个知识库的文档 —— 先跑 ingest")
    return DocumentPaths(mapping, kb=kb)


async def _run_one_ablation(
    *,
    name: str,
    questions: Sequence[RetrievalQuestion],
    retriever: HybridRetriever,
    paths: DocumentPaths,
    embedder: Any,
    reranker: Any,
    settings: Settings,
    top_k: int,
    fulltext_min: float,
) -> AblationReport:
    """跑一组消融。每组独立循环，保证四组用的是**同一批问句、同一个库**。"""
    results: list[QuestionResult] = []

    for item in questions:
        result = QuestionResult(id=item.id, question=item.question, must_hit=item.must_hit)
        try:
            embedding: list[float] | None = None
            if name in ("vector", "rrf", "rerank") and embedder is not None:
                # ⚠️ 同一题在四组消融里会各来一次 —— 问句向量必须一模一样，
                #    否则"消融"就变成了"换了个问法"。缓存同时也省掉 3/4 的 embedding 调用。
                embedding = _EMBED_CACHE.get(item.question)
                if embedding is None:
                    embedding = (await embedder.embed([item.question]))[0]
                    _EMBED_CACHE[item.question] = embedding

            hits: list[RetrievedChunk]
            if name == "fulltext":
                hits = await retriever._fulltext(
                    item.question,
                    limit=settings.retrieve_top_k,
                    min_similarity=fulltext_min,
                )
            elif name == "vector":
                hits = (
                    await retriever._vector(embedding, limit=settings.retrieve_top_k)
                    if embedding
                    else []
                )
            else:
                hits, _ = await retriever.search(
                    query=item.question,
                    query_embedding=embedding,
                    top_k=settings.retrieve_top_k,
                    rrf_k=settings.rrf_k,
                    fulltext_min_similarity=fulltext_min,
                )
                if name == "rerank" and reranker is not None and hits:
                    # ⚠️ 这里故意多要几个（10）而不是线上的 `rerank_top_n`（5）：
                    #    只留 5 个就测不了 MRR@10，而 MRR 正是"第一名对不对"的度量。
                    ranked = await reranker.rerank(
                        item.question,
                        [hit.content for hit in hits],
                        top_n=min(len(hits), 10),
                    )
                    by_index = dict(ranked)
                    reordered = [
                        hits[index]
                        for index in sorted(by_index, key=lambda i: by_index[i], reverse=True)
                        if 0 <= index < len(hits)
                    ]
                    hits = reordered

            docs = paths.doc_ranking(hits, limit=top_k)
            result.top_docs = docs
            result.rank = _rank_of(docs, item.must_hit)
        except ModelAPIError as exc:  # 模型侧挂了要记下来，不能算成"0 分"
            result.error = f"ModelAPIError: {exc}"
        results.append(result)

    return AblationReport(name=name, results=results)


def _print_report(reports: Sequence[AblationReport], *, top_k: int, total: int) -> None:
    print()
    print(
        f"{'消融':<10} {'hit@' + str(top_k):>8} {'MRR@10':>8} {'命中':>8} {'平均首中位次':>14} {'出错':>6}"
    )
    print("-" * 62)
    for rep in reports:
        mean_rank = rep.mean_rank()
        rank_text = f"{mean_rank:.2f}" if mean_rank is not None else "—"
        print(
            f"{rep.name:<10} {rep.hit_at(top_k):>8.3f} {rep.mrr_at(10):>8.3f} "
            f"{rep.hit_count:>5}/{total:<2} {rank_text:>14} {rep.errors:>6}"
        )
    print()

    for rep in reports:
        misses = [r for r in rep.results if r.rank is None and r.error is None]
        if not misses:
            continue
        print(f"【{rep.name}】没进 top-{top_k} 的 {len(misses)} 题：")
        for r in misses:
            wanted = " / ".join(r.must_hit)
            got = " | ".join(r.top_docs[:3]) or "（空）"
            print(f"  - {r.id}  期望 {wanted}")
            print(f"            实际 {got}")
        print()


def _dump(
    reports: Sequence[AblationReport], *, top_k: int, settings: Settings, meta: dict[str, Any]
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"retrieval-{stamp}.json"
    payload = {
        "kind": "retrieval",
        "generated_at": stamp,
        "top_k": top_k,
        "config": {
            "retrieve_top_k": settings.retrieve_top_k,
            "rrf_k": settings.rrf_k,
            "fulltext_min_similarity": settings.fulltext_min_similarity,
            "rerank_model": settings.rerank_model,
            "embedding_model": settings.embedding_model,
        },
        "meta": meta,
        "ablations": [
            {
                "name": rep.name,
                "hit_at_k": round(rep.hit_at(top_k), 4),
                "mrr_at_10": round(rep.mrr_at(10), 4),
                "hit_count": rep.hit_count,
                "mean_rank": rep.mean_rank(),
                "errors": rep.errors,
                "questions": [
                    {
                        "id": r.id,
                        "must_hit": r.must_hit,
                        "rank": r.rank,
                        "top_docs": r.top_docs,
                        **({"error": r.error} if r.error else {}),
                    }
                    for r in rep.results
                ],
            }
            for rep in reports
        ],
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return out


async def _run_retrieval(args: argparse.Namespace) -> int:
    settings = get_settings()
    questions = load_retrieval_questions(QUESTIONS_DIR / "retrieval.yaml")
    if args.limit:
        questions = questions[: args.limit]

    names = ABLATIONS if args.only == "all" else (args.only,)
    #: 全文路的门槛可在命令行覆盖 —— 用来做"把 0.333 的平局滤掉会不会更好"这类实验。
    ft_min = (
        args.fulltext_min if args.fulltext_min is not None else settings.fulltext_min_similarity
    )

    _, embedder, reranker = build_clients(settings)
    needs_embedding = bool({"vector", "rrf", "rerank"} & set(names))
    if needs_embedding and embedder is None:
        raise SystemExit("没有 EMBEDDING_API_KEY，向量路跑不了")
    if "rerank" in names and reranker is None:
        raise SystemExit("RERANK_ENABLED 没开或没有 RERANK_API_KEY，精排跑不了")

    factory = get_session_factory()
    reports: list[AblationReport] = []
    try:
        async with factory() as session:
            tenant_id = await _resolve_tenant_id(session, settings.default_tenant_id)
            paths = await _document_paths(session, tenant_id, kb=args.kb)
            retriever = HybridRetriever(session, tenant_id=tenant_id)
            print(
                f"题库 {len(questions)} 题 · 知识库 {args.kb} · "
                f"候选 {settings.retrieve_top_k} · 文档 {paths.document_count}"
            )
            for name in names:
                print(f"→ 跑 {name} …")
                reports.append(
                    await _run_one_ablation(
                        name=name,
                        questions=questions,
                        retriever=retriever,
                        paths=paths,
                        embedder=embedder,
                        reranker=reranker,
                        settings=settings,
                        top_k=args.top_k,
                        fulltext_min=ft_min,
                    )
                )
    finally:
        await dispose_engine()
        await aclose_http_client()

    _print_report(reports, top_k=args.top_k, total=len(questions))
    out = _dump(
        reports,
        top_k=args.top_k,
        settings=settings,
        meta={"kb": args.kb, "questions": len(questions), "ablations": list(names)},
    )
    print(f"全文门槛 fulltext_min_similarity = {ft_min}")
    print(f"结果已存：{out.relative_to(REPO_ROOT)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="grounded-qa 评测 runner")
    parser.add_argument("--layer", default="retrieval", choices=["retrieval"])
    parser.add_argument("--kb", default="senyu", help="知识库前缀（ingest 时 --kb 的值）")
    parser.add_argument("--top-k", type=int, default=5, help="hit@k 的 k")
    parser.add_argument("--only", default="all", choices=["all", *ABLATIONS], help="只跑某一组消融")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（试水用）")
    parser.add_argument(
        "--fulltext-min",
        type=float,
        default=None,
        help="覆盖 fulltext_min_similarity（做门槛实验用）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run_retrieval(args))


if __name__ == "__main__":
    raise SystemExit(main())
