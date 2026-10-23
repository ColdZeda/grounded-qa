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
import os
import random
import re
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models.knowledge import Chunk, Document
from app.db.models.tenant import Tenant
from app.db.session import dispose_engine, get_session_factory
from app.rag.pipeline import Answer, RagPipeline, build_clients
from app.rag.providers import ChatClient, ModelAPIError, aclose_http_client
from app.rag.retrieval import HybridRetriever, RetrievedChunk
from app.rag.vision import build_image_client

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_DIR = REPO_ROOT / "evals" / "questions"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
#: 图片 fixture 与它的真值（都进 git；镜像里排除，见 `.dockerignore`）
IMAGES_DIR = REPO_ROOT / "evals" / "fixtures" / "images"
IMAGES_META = REPO_ROOT / "evals" / "fixtures" / "images.meta.yaml"

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
            must_hit=list(item.get("must_hit", [])),
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
    #: top-1 候选的**该路原分**（向量路=余弦，精排路=rerank 分数）。
    #: 用来标定"两套阈值"（PLAN §8.4 D2）—— 这是消融跑批的副产品，不额外花钱。
    top1_score: float | None = None
    top1_doc: str = ""
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
                    # ⚠️ 必须把 rerank 分**写回** RetrievedChunk，不能只改顺序：
                    #    原来这里直接重排原对象，`.score` 还是 search() 给的那一路分（余弦），
                    #    于是"精排 top-1 分数"其实是余弦 —— 2026-10-17 用它标定阈值时踩到，
                    #    得到"中位数 0.539 < 阈值 0.60"这种和事实相反的结论（真实 rerank 分是 0.95+）。
                    reranked_chunks: list[RetrievedChunk] = []
                    for index in sorted(by_index, key=lambda i: by_index[i], reverse=True):
                        if not (0 <= index < len(hits)):
                            continue
                        base = hits[index]
                        reranked_chunks.append(
                            RetrievedChunk(
                                chunk_id=base.chunk_id,
                                document_id=base.document_id,
                                ordinal=base.ordinal,
                                content=base.content,
                                score=by_index[index],
                            )
                        )
                    hits = reranked_chunks

            if hits:
                result.top1_score = hits[0].score
                result.top1_doc = paths.path_of(hits[0].document_id)
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
        scores = [r.top1_score for r in rep.results if r.top1_score is not None]
        if not scores:
            continue
        scores.sort()
        print(
            f"【{rep.name}】top-1 原分：n={len(scores)}  min={scores[0]:.3f}  "
            f"中位={statistics.median(scores):.3f}  max={scores[-1]:.3f}"
        )
    print()

    for rep in reports:
        misses = [r for r in rep.results if r.rank is None and r.error is None and r.must_hit]
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
    reports: Sequence[AblationReport],
    *,
    top_k: int,
    settings: Settings,
    meta: dict[str, Any],
    ref_time: date | None = None,
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
            # ⚠️ 必须记下来：`all` 与 `current` 是**两支尺子**，分数不同但都合法。
            #    不记的话两组结果文件长得一样，事后分不清哪份是哪份（2026-10-22 踩过）。
            "retrieval_scope": settings.retrieval_scope,
            # ⚠️ 同理，`ref_time` 也必须记：同一个 `--scope current` 之下，
            #    按 2024-03 和按今天召回的是**两套不同的文档**（v1.3 B2）。
            "ref_time": ref_time.isoformat() if ref_time else None,
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
                        "top1_score": r.top1_score,
                        "top1_doc": r.top1_doc,
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
    qpath = Path(args.questions) if args.questions else QUESTIONS_DIR / "retrieval.yaml"
    questions = load_retrieval_questions(qpath)
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
            # ⚠️ 评测直接建 retriever，**不走 pipeline** —— 所以 scope 必须在这里显式传，
            #    否则 `--scope` 只改了配置、这里仍然用构造函数的默认值（静默失效，2026-10-22 踩中）。
            #    `ref_time` 同理：`--ref-time` 不往这里传 = 这个开关等于没加。
            retriever = HybridRetriever(
                session,
                tenant_id=tenant_id,
                scope=settings.retrieval_scope,
                ref_time=args.ref_time,
            )
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
        ref_time=args.ref_time,
    )
    print(f"全文门槛 fulltext_min_similarity = {ft_min}")
    print(f"参照时刻 ref_time = {args.ref_time or '（今天）'}")
    print(f"结果已存：{out.relative_to(REPO_ROOT)}")
    return 0


# ---------------------------------------------------------------- 生成层 / 拒答层

#: 中文数字 → 数值。只收语料里真会出现的写法（"两"最常见：`2 年` 常被写成 `两年`）。
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

#: 单位 → **(比较用的族, 换算到基准单位的倍数)**。
#:
#: ⚠️ **只换算有定义或足够硬的**：`1 年 = 12 个月`、`1 天 = 24 小时`。
#:    **月 ↔ 天不换算**（`30 天 ≠ 1 个月`，2 月只有 28 天）——
#:    拿约等号去判分，会把"120 天"和"4 个月"判成同一个，方向正好反了。
_UNITS: dict[str, tuple[str, float]] = {
    "个自然日": ("hour", 24.0),
    "自然日": ("hour", 24.0),
    "个工作日": ("workday", 1.0),
    "工作日": ("workday", 1.0),
    "小时": ("hour", 1.0),
    "个月": ("month", 1.0),
    "天": ("hour", 24.0),
    "日": ("hour", 24.0),
    "月": ("month", 1.0),
    "年": ("month", 12.0),
    "%": ("percent", 1.0),
}

#: 钟点（`09:00`）。⚠️ 它是**格式**不是政策数值：拆成 9 和 00 之后，
#: 回答里的 `0900`（`_norm` 会把冒号抹掉）永远对不上 —— 实测把一道**全对**的题
#: 判成错（知识库 B gen-15 客服响应时段）。所以核对数字前先把钟点整段抹掉。
_CLOCK = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")

_UNIT_ALT = "|".join(sorted((re.escape(unit) for unit in _UNITS), key=len, reverse=True))
_CN_NUMBER = "".join(_CN_DIGITS) + "十"
_NUM = rf"\d+(?:\.\d+)?|[{_CN_NUMBER}]{{1,3}}"

#: "数字 （可选区间） （可选单位）"。
#: ⚠️ 区间必须**连着一起来**：`3~5 个工作日` 里那个 `3` 属于"工作日"，
#:    单独抽出来会变成"裸数字 3"，于是要点判不过一个完全正确的回答。
_NUMERIC = re.compile(
    rf"(?P<num>{_NUM})\s*"
    rf"(?:(?P<sep>[~\-–—至到])\s*(?P<high>{_NUM})\s*)?"
    rf"(?P<unit>{_UNIT_ALT})?"
)

#: 拉丁词（型号 / 平台名：`PayPal`、`SF10001` 里的字母部分）。
#: ⚠️ 数字写在拉丁词里的（`QR30`）两样都要求 —— 判分宁可少认一个"对"，不要多认一个"错"。
_LATIN = re.compile(r"[A-Za-z]{2,}")


def _to_number(raw: str) -> float | None:
    """`3` / `12.5` / `二十四` / `十五` / `十` → 数值；认不出 → `None`。"""
    if raw[:1].isdigit():
        return float(raw)
    if not all(ch in _CN_DIGITS or ch == "十" for ch in raw):
        return None
    if "十" not in raw:
        return float(_CN_DIGITS[raw]) if len(raw) == 1 else None
    head, _, tail = raw.partition("十")
    if head and head not in _CN_DIGITS:
        return None
    if tail and tail not in _CN_DIGITS:
        return None
    tens = _CN_DIGITS[head] if head else 1
    ones = _CN_DIGITS[tail] if tail else 0
    return float(tens * 10 + ones)


def _numeric_atoms(text: str) -> list[tuple[str, float]]:
    """把文字里的"数字 ＋ 单位"抽成**可比较的原子**：`(族, 换算后的数值)`。

    ⚠️ 这比"把数字当字符串在回答里找一遍"**严**，也比它**松**，两边都是必需的：

      · **更严**：要点写 `家具主体 2 年`，回答却写 `24 个月`（或只提了 `2024` 年），
        旧写法只要回答里有个 `2` 就算命中 —— **同义改写反而蒙混过关**；
      · **更松**：`两年` / `2年` / `24 个月` 是同一个意思，现在都认。

    📌 这就是 v1.4 B1（#9「判分改数字集合比对」）：判的是**集合**，不是"找个子串"。
    """
    atoms: list[tuple[str, float]] = []
    # 钟点先整段抹掉（理由见 `_CLOCK`）
    text = _CLOCK.sub(" ", text)
    for match in _NUMERIC.finditer(text):
        raw = match.group("num")
        chinese = not raw[:1].isdigit()
        unit = match.group("unit")
        # ⚠️ **中文数字只有带单位才算数量**：`一层`、`一次`、`同一`、`统一` 里的"一"
        #    是量词/副词，不是数据。不挡的话，要点"多一层 `item` 键"会要求回答里出现数字 1，
        #    一个完全正确的回答会被判错（实测：知识库 A 的 20 题里误杀 4 题）。
        if chinese and unit is None:
            continue
        if chinese and _to_number(raw) is None:
            continue
        high_raw = match.group("high")
        numbers = [raw] + ([high_raw] if high_raw else [])
        for piece in numbers:
            value = _to_number(piece)
            if value is None:
                continue
            if unit is None:
                atoms.append(("bare", value))
            else:
                family, factor = _UNITS[unit]
                atoms.append((family, value * factor))
    return atoms


def _strip_citation_marks(text: str) -> str:
    """去掉 `[1]` `[2]` 这类引用标记。

    ⚠️ 必须去掉：回答里的 `[1]` 会把数字 "1" 送进匹配，
       于是要点里写 "电子件 1 年" 这种**其实没答到**的项也会被误判为命中。
    """
    return re.sub(r"\[\d+\]", "", text)


def _norm(text: str) -> str:
    """只抹掉空白与中文标点，**保留数字、%、.、~、-、/ 与拉丁字母**。

    ⚠️ 不要把 `-` `/` `~` 也抹掉：抹了之后 "3~5" 会变成 "35"，
       于是"3~5 个工作日"能被"35 天"这种无关文本命中 —— 假阳性比漏判更伤评测。
    """
    return re.sub(r"[\s，,、；;：:。！!？?（）()【】\[\]「」『』\"'“”‘’—－]", "", text).lower()


def _bigrams(text: str) -> set[str]:
    """只取**中文**字符的相邻二元组，作为"语义指纹"。

    为什么不用逐字包含：中文里同义/近义的替换太常见 ——
    "运费**与**关税" vs "运费**和**关税"、"用积分抵扣" vs "用积分**或优惠券**抵扣"，
    逐字匹配全判不中，于是**判分器把满分答案判成错**（2026-10-17 实测踩到）。
    二元组覆盖率对这类插入/替换是容忍的，同时又比"整句相似度"细。
    """
    # ⚠️ 必须**按连续中文段**取，不能把所有汉字先捞出来再拼：
    #    "总则 7 天" 捞出来是 ["总","则","天"]，会造出"则天"这种跨数字的假二元组，
    #    于是要点永远凑不够覆盖率 —— 这是 2026-10-17 实测踩到的第二个判分坑。
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return grams


def _point_hit(point: str, answer_norm: str) -> tuple[bool, bool]:
    """一条要点是否命中。返回 `(命中, 是否"弱判定")`。

    判定规则（刻意简单、可解释，**不用 LLM 当裁判**）：

    1. 要点里的**数字＋单位**与**拉丁词**必须**全部**在回答里 —— 硬条件。
       数字按 `(族, 换算后数值)` 做**集合比对**：`2 年` = `24 个月`、`48 小时` = `2 天`；
    2. 中文部分看**二元组覆盖率**：要点里的中文二元组有多少比例出现在回答里，
       阈值 0.6（去掉括号补充说明后再算）。

    ⚠️ "弱判定" = 要点里既没数字也没拉丁词，只靠二元组覆盖 ——
        这种判定天然偏松，输出里单独计数，别和数字型要点同等看待。
    ⚠️ 第 1 条为什么必须是**硬**条件：数字是政策里**最不可替换**的证据
        （"7 天"和"30 天"不是同义词）；中文部分才允许同义改写（"退货"与"退换"等价），
        那正是二元组覆盖率的用武之地。
    """
    plain = re.sub(r"[（(][^）)]*[）)]", "", point)
    atoms = _numeric_atoms(plain)
    latin = {word.lower() for word in _LATIN.findall(plain)}
    if atoms or latin:
        got_atoms = set(_numeric_atoms(answer_norm))
        # ⚠️ 拉丁词仍按**子串**判（不是词集合）：`_norm` 会把空格抹掉，
        #    `fastapi dev` 在回答里会粘成 `fastapidev` —— 按词比会把**答对的**判成错
        #    （实测：知识库 A 的 20 题里误杀 3 题）。
        return (
            all(atom in got_atoms for atom in atoms) and all(word in answer_norm for word in latin),
            False,
        )

    want = _bigrams(plain)
    if not want:
        return False, True
    got = _bigrams(answer_norm)
    coverage = len(want & got) / len(want)
    return coverage >= 0.6, True


class SpyChat:
    """把 `ChatClient` 包一层，数**生成模型到底被调了几次**，其余原样透传。

    ⚠️ 拒答层的核心断言不是"回答看起来像拒答"，而是**根本没花 token**。
       只看文案的话，"模型自己决定不答"也能骗过判分 —— 那正是本项目要避免的设计。
    ⚠️ 必须**透传**而不是吞掉：生成层要的是真实回答，只有拒答层才断言"0 次"。
    """

    def __init__(self, inner: ChatClient) -> None:
        self._inner = inner
        self.calls = 0

    # ⚠️ 必须**透传**而不是吞掉：生成层要的是真实回答，只有拒答层才断言"0 次"。
    # ⚠️ **`usage` 也要透传**（2026-10-20 修）：`ChatClient.stream()` 在加 token 计量时
    #    多了一个 `usage` 关键字参数，而这个包装器的签名没跟着改 —— 于是
    #    `--layer generation` 会直接 `TypeError: stream() got an unexpected keyword
    #    argument 'usage'`，而且它不在 `ModelAPIError` 的捕获范围内，整批会崩在那一行。
    #    教训：**包装器的签名要盯住被包装对象**，否则对方"加个新参数"就静默炸在别的模块。
    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        usage: dict[str, int] | None = None,
    ) -> Any:
        self.calls += 1
        async for piece in self._inner.stream(messages, usage=usage):
            yield piece


@dataclass
class AnswerResult:
    id: str
    question: str
    refused: bool
    llm_calls: int
    cited: list[str]
    text: str
    #: pipeline 判拒答时**真正用的**置信度（rerank 分或余弦）。
    #: 标定阈值（D2）必须看这个 —— 我自己算的 top-1 与它口径不同就会得到相反结论。
    confidence: float | None = None
    points: list[tuple[str, bool, bool]] = field(default_factory=list)  # (要点, 命中, 弱判定)
    forbidden_hit: list[str] = field(default_factory=list)
    cite_ok: bool | None = None
    must_say_ok: bool | None = None
    ok: bool | None = None
    expect_refuse: bool | None = None
    error: str | None = None

    @property
    def points_hit(self) -> int:
        return sum(1 for _, hit, _ in self.points if hit)

    @property
    def points_total(self) -> int:
        return len(self.points)

    @property
    def weak_points(self) -> int:
        return sum(1 for _, _, weak in self.points if weak)

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if self.forbidden_hit:
            return False
        if self.cite_ok is False:
            return False
        return self.points_hit == self.points_total


class RandomEvidencePipeline(RagPipeline):
    """**随机证据组**（v1.4 B3）：同一个提示词、同一段代码，只把检索结果换成 5 段无关内容。

    为什么需要它：闭卷 vs 开卷只能证明"**给了证据**比没给强"，但那份证据里混着两样东西 ——
    **检索找对了**（内容相关）和**提示词要求引用资料**（格式上有抓手）。要拆开，
    就得有第三组：**开卷的提示词 ＋ 无关的证据**。

        闭卷 → 随机证据 → 真证据
              └─ 提示词 ─┘└─ 检索 ─┘

    ⚠️ 只覆盖 `_retrieve` 一个方法 —— 提示词、拒答、引用、落库全都走原路径，
       否则差值里会混进"两套实现"的噪音（和闭卷那条纪律一样，见 §9.34 ②）。
    ⚠️ 置信度给 `None`：没有可信的相关性分数，就不该拿它去判拒答
       （`RagPipeline._should_refuse` 对"没分数但有命中"的结论是**照答**，
       这正是我们要的 —— 随机证据组要和开卷组**一样地尽力回答**）。
    """

    def __init__(self, *, evidence: Sequence[RetrievedChunk], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._evidence = list(evidence)

    async def _retrieve(  # type: ignore[override]
        self, question: str, *, ref_time: date | None = None
    ) -> tuple[list[RetrievedChunk], float | None, str]:
        return list(self._evidence), None, "none"


def _random_evidence(
    pool: Sequence[RetrievedChunk],
    *,
    exclude_paths: set[str],
    path_of: Any,
    count: int,
    rng: random.Random,
) -> list[RetrievedChunk]:
    """从库里随机抽 `count` 段，**排掉这道题的标准出处**，长度尽量和真证据接近。

    三条保证（`PLAN §8.7` ③）缺一条这个实验就白做：
      ① 固定随机种子（`--seed`）—— 否则"某次运气好"无法复现；
      ② **同样 `count` 段、长度相近** —— 否则"证据更少/更短"会混进结论；
      ③ **排除同文件** —— 不然随机抽到正解所在文档，这组就不是"无关证据"了。
    """
    usable = [chunk for chunk in pool if path_of(chunk.document_id) not in exclude_paths]
    if len(usable) < count:
        usable = list(pool)
    if not usable:
        return []
    lengths = sorted(len(chunk.content) for chunk in usable)
    median = lengths[len(lengths) // 2]
    # 长度相近：中位数 ±40%。抽不够就放宽，宁可长度略偏也不许少于 count 段。
    near = [chunk for chunk in usable if median * 0.6 <= len(chunk.content) <= median * 1.6]
    candidates = near if len(near) >= count else usable
    rng.shuffle(candidates)
    return candidates[:count]


def _load_items(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{path} 的顶层结构不是列表")
    return raw


async def _run_answer_layer(
    args: argparse.Namespace, *, layer: str, mode: str = "open"
) -> list[AnswerResult]:
    settings = get_settings()
    if args.questions:
        # 换一个题集（知识库 A 的题集与 B 库结构相同，只是文件不同）。
        # ⚠️ 相对路径按**仓库根**解析，不按当前目录 —— 否则从 `evals\` 里跑就找不到。
        qpath = Path(args.questions)
        if not qpath.is_absolute():
            qpath = (REPO_ROOT / qpath).resolve()
        items = _load_items(qpath)
    elif layer == "generation":
        items = _load_items(QUESTIONS_DIR / "generation.yaml")
    else:
        items = _load_items(QUESTIONS_DIR / "refusal.yaml")
    if args.limit:
        items = items[: args.limit]

    chat, embedder, reranker = build_clients(settings)
    spy = SpyChat(chat)
    factory = get_session_factory()
    results: list[AnswerResult] = []

    try:
        async with factory() as session:
            tenant_id = await _resolve_tenant_id(session, settings.default_tenant_id)
            paths = await _document_paths(session, tenant_id, kb=args.kb)
            print(f"题库 {len(items)} 题 · 层 {layer} · 知识库 {args.kb}")
            pool: list[RetrievedChunk] = []
            if mode == "random":
                rows = (
                    await session.execute(
                        select(
                            Chunk.id,
                            Chunk.document_id,
                            Chunk.ordinal,
                            Chunk.content,
                        ).where(Chunk.tenant_id == tenant_id)
                    )
                ).all()
                pool = [
                    RetrievedChunk(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        ordinal=ordinal,
                        content=content,
                        score=0.0,
                    )
                    for chunk_id, document_id, ordinal, content in rows
                ]
                print(f"随机证据池：{len(pool)} 段（种子 {args.seed}）")
            for index, item in enumerate(items, start=1):
                spy.calls = 0
                # ⚠️ 随机证据组走 `RandomEvidencePipeline`（只换检索结果），
                #    其余模式一律用**同一条** `RagPipeline` —— 对照组和被试组的差异里
                #    不许混进"两套实现"的噪音。
                pipeline_cls: type[RagPipeline] = (
                    RandomEvidencePipeline if mode == "random" else RagPipeline
                )
                evidence: list[RetrievedChunk] = []
                if mode == "random":
                    evidence = _random_evidence(
                        pool,
                        exclude_paths=set(item.get("must_hit", [])),
                        path_of=paths.path_of,
                        count=settings.rerank_top_n,
                        # 每题一个稳定的种子：同一批题重跑抽到的是同一批段落
                        # ⚠️ 这里**要的就是可复现的伪随机**（不是密码学随机），
                        #    所以对 S311 明确豁免：随机性只用在对结果做对照上。
                        rng=random.Random(f"{args.seed}:{item['id']}"),  # noqa: S311
                    )
                pipeline = pipeline_cls(
                    session=session,
                    tenant_id=tenant_id,
                    chat=spy,  # type: ignore[arg-type]
                    embedder=embedder,
                    reranker=reranker,
                    settings=settings,
                    # 闭卷：同一段代码，只是不检索（见 `RagPipeline` 的 `closed_book`）
                    closed_book=mode == "closed",
                    # 冻结参照时刻（v1.3 B2）：不传则用今天，历史版本只在它当时有效
                    ref_time=args.ref_time,
                    **({"evidence": evidence} if mode == "random" else {}),
                )
                result = AnswerResult(
                    id=item["id"],
                    question=item["question"],
                    refused=False,
                    llm_calls=0,
                    cited=[],
                    text="",
                )
                try:
                    final: Answer | None = None
                    async for event in pipeline.answer(question=item["question"]):
                        if isinstance(event, Answer):
                            final = event
                    if final is None:
                        result.error = "没有拿到最终 Answer 事件"
                    else:
                        result.text = final.text
                        result.refused = final.refused
                        result.confidence = final.confidence
                        result.cited = [paths.path_of(c.document_id) for c in final.citations]
                except ModelAPIError as exc:
                    result.error = f"ModelAPIError: {exc}"
                result.llm_calls = spy.calls

                answer_norm = _norm(_strip_citation_marks(result.text))
                if layer == "generation":
                    for point in item.get("required_points", []):
                        hit, weak = _point_hit(point, answer_norm)
                        result.points.append((point, hit, weak))
                    must_cite = item.get("must_cite", [])
                    # ⚠️ 闭卷模式下**不判引用**：它压根没有检索，判"引用对"等于判它作弊。
                    #    `cite_ok` 保持 None，`passed` 的判据就只看要点与禁语 —— 两组口径一致。
                    if must_cite and mode == "open":
                        result.cite_ok = bool(set(must_cite) & set(result.cited))
                    for banned in item.get("must_not_say", []):
                        if _norm(banned) and _norm(banned) in answer_norm:
                            result.forbidden_hit.append(banned)
                else:
                    expect_refuse = bool(item.get("should_refuse", True))
                    result.expect_refuse = expect_refuse
                    must_say = item.get("must_say", [])
                    if must_say:
                        result.must_say_ok = any(_norm(s) in answer_norm for s in must_say)
                    ok = result.refused == expect_refuse and result.must_say_ok is not False
                    result.ok = ok
                    mark = "✓" if ok else "✗"
                    token_note = (
                        "" if result.llm_calls == 0 else f"  ⚠️ 调了模型 {result.llm_calls} 次"
                    )
                    print(
                        f"  [{index:>2}/{len(items)}] {mark} {result.id}  refused={result.refused}{token_note}"
                    )
                results.append(result)

                if args.show_answers and layer == "generation":
                    print(f"\n--- {result.id} · {result.question}")
                    print(f"    confidence={result.confidence} refused={result.refused}")
                    print(f"    {result.text[:400]}")
                    print(f"    引用：{' | '.join(result.cited[:3])}")
                if result.error:
                    print(f"  ⚠️ {result.id} {result.error}")
    finally:
        await dispose_engine()
        await aclose_http_client()

    return results


def _report_answer_layer(
    results: Sequence[AnswerResult],
    *,
    layer: str,
    mode: str = "open",
    settings: Settings,
    args: argparse.Namespace,
    started: datetime,
) -> dict[str, Any]:
    print()
    #: 三种模式的标题。⚠️ 随机证据组的措辞要能一眼看出"**证据是无关的**"，
    #:    否则读报告的人会以为它也检索了（v1.4 B3）。
    tags = {
        "open": "开卷（有检索证据）",
        "closed": "闭卷（无检索，凭模型自身知识）",
        "random": "随机证据（开卷提示词 ＋ 无关段落）",
    }
    tag = tags.get(mode, mode)
    print(f"===== {args.kb} · {layer} · {tag} =====")
    if layer == "generation":
        ok = sum(1 for r in results if r.passed)
        strict = [r for r in results if r.weak_points == 0]
        strict_ok = sum(1 for r in strict if r.passed)
        # ⚠️ 只有开卷判引用：闭卷没检索，随机证据组的"引用"是随机的，判它等于判它作弊
        rules = "要点全中 ＋ 引用对 ＋ 无禁语" if mode == "open" else "要点全中 ＋ 无禁语"
        print(f"生成层：**{ok}/{len(results)} 题全通过**（{rules}）")
        if strict:
            print(
                f"  其中「全部要点都是数字型」的 {len(strict)} 题：{strict_ok}/{len(strict)} 通过"
            )
        weak_total = sum(r.weak_points for r in results)
        if weak_total:
            print(f"  ⚠️ 有 {weak_total} 条要点属于「弱判定」（无数字/拉丁词），别当同等证据")
        for r in results:
            if r.passed:
                continue
            why: list[str] = []
            if r.error:
                why.append(r.error)
            missing = [p for p, hit, _ in r.points if not hit]
            if missing:
                why.append(f"要点缺 {len(missing)}/{r.points_total}：{missing[0][:40]}")
            if r.cite_ok is False:
                why.append(f"引用不匹配（实际 {r.cited[:2]}）")
            if r.forbidden_hit:
                why.append(f"出现禁语：{r.forbidden_hit}")
            print(f"  ✗ {r.id}  " + "；".join(why))
    else:
        correct = sum(1 for r in results if r.ok)
        clean = sum(1 for r in results if r.llm_calls == 0)
        print(f"拒答层：**{correct}/{len(results)} 题行为正确**")
        print(f"  **没调生成模型的有 {clean}/{len(results)} 题**（这才是「省 token」的证据）")
        for r in results:
            # ⚠️ 只有「本该拒答」的题调了模型才算违规；should_refuse=False 的题本来就该答，
            #    调模型是正常的 —— 这里写死"一律告警"会把正常行为报成错。
            if r.llm_calls != 0 and r.expect_refuse is not False:
                print(f"  ⚠️ {r.id} 本该拒答却调了生成模型 {r.llm_calls} 次")

    out = RESULTS_DIR / f"{layer}-{mode}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "kind": layer,
                "mode": mode,
                "generated_at": started.strftime("%Y%m%dT%H%M%SZ"),
                "kb": args.kb,
                "questions": args.questions or f"{layer}.yaml",
                "llm_model": settings.llm_model,
                "results": [
                    {
                        "id": r.id,
                        "question": r.question,
                        "passed": r.passed,
                        "refused": r.refused,
                        "confidence": r.confidence,
                        "llm_calls": r.llm_calls,
                        "cited": r.cited,
                        "points": [
                            {"point": p, "hit": hit, "weak": weak} for p, hit, weak in r.points
                        ],
                        "forbidden_hit": r.forbidden_hit,
                        "cite_ok": r.cite_ok,
                        "must_say_ok": r.must_say_ok,
                        "ok": r.ok,
                        "expect_refuse": r.expect_refuse,
                        "text": r.text,
                        **({"error": r.error} if r.error else {}),
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n结果已存：{out.relative_to(REPO_ROOT)}")
    return _summarize(results, layer=layer, mode=mode, kb=args.kb, out=out)


def _summarize(
    results: Sequence[AnswerResult],
    *,
    layer: str,
    mode: str,
    kb: str,
    out: Path,
) -> dict[str, Any]:
    """把一批结果压成对照表要用的几个数。**只统计，不重判。**

    ⚠️ 指标口径（两组必须完全一致，否则"差值"没有意义）：

        · **要点全中**：这题的每条要点都命中（不看引用）
        · **出现禁语**：`must_not_say` 命中了任意一条 —— **这是幻觉的直证**
        · **引用对**：只有开卷有意义；闭卷 `cite_ok` 恒为 `None`，这里如实报 `—`

    为什么把"要点全中"和 `passed` 分开：`passed` 还包含"引用对"这一条，
    闭卷拿不到引用、开卷要看引用 —— 用 `passed` 做跨组比较就是拿两把尺子量。
    """
    total = len(results)
    errors = sum(1 for r in results if r.error is not None)
    points_total = sum(r.points_total for r in results)
    points_hit = sum(r.points_hit for r in results)
    all_points = sum(1 for r in results if r.points_total and r.points_hit == r.points_total)
    forbidden_q = sum(1 for r in results if r.forbidden_hit)
    cite_checked = sum(1 for r in results if r.cite_ok is not None)
    cite_ok = sum(1 for r in results if r.cite_ok)
    #: 回答里出现了数字的题数。
    #: ⚠️ 这是"幻觉"的**弱证据**，但它挡住了最容易被误读的一种解读：
    #:    "闭卷答不上来" ≠ "闭卷在回避"。恰恰相反 —— 它照样给出具体数字（满 99 元包邮、
    #:    同城 1~2 个工作日），只是**那些数字是编的**。而 `must_not_say` 那种整句子串
    #:    匹配抓不住改写后的编造（见 `architecture.md` §9.34 的判分局限）。
    answers_with_digits = sum(1 for r in results if re.search(r"\d", r.text))
    return {
        "layer": layer,
        "mode": mode,
        "kb": kb,
        "total": total,
        "errors": errors,
        "all_points": all_points,
        "all_points_rate": round(all_points / total, 4) if total else 0.0,
        "points_hit": points_hit,
        "points_total": points_total,
        "points_rate": round(points_hit / points_total, 4) if points_total else 0.0,
        "forbidden_questions": forbidden_q,
        "forbidden_rate": round(forbidden_q / total, 4) if total else 0.0,
        "answers_with_digits": answers_with_digits,
        "cite_checked": cite_checked,
        "cite_ok": cite_ok,
        "result_file": str(out.relative_to(REPO_ROOT)),
    }


def _rescore_generation(args: argparse.Namespace) -> int:
    """用**已经存下来的回答**重算判分，不再调模型。

    ⚠️ 为什么需要它：改判分规则时若重跑一遍，**回答也跟着变了** ——
       那就分不清"分变了"是因为尺子还是因为模型。rescore 保证"同一批回答、只换尺子"。
    """
    path = Path(args.rescore)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        raise SystemExit(f"找不到结果文件：{path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    # ⚠️ 题集要**从结果文件里读**（`payload["questions"]`），不能假设是 `generation.yaml`：
    #    知识库 A 用的是 `generation-fastapi.yaml`，写死的话每题都对不上题号，
    #    于是"重判"出来的是一份 **0/0 的空报告**，而且**照样存档、照样显示成功**
    #    （2026-10-23 实测踩到 —— 又一个"静默失效"）。
    questions_name = str(payload.get("questions") or "generation.yaml")
    questions_path = Path(questions_name)
    if not questions_path.is_absolute():
        questions_path = (REPO_ROOT / questions_path).resolve()
    if not questions_path.exists():
        questions_path = QUESTIONS_DIR / Path(questions_name).name
    if not questions_path.exists():
        raise SystemExit(f"找不到题集：{questions_name}")
    print(f"题集：{questions_path.relative_to(REPO_ROOT)}")
    items = {item["id"]: item for item in _load_items(questions_path)}
    # ⚠️ 模式与知识库也**从结果文件里读**：重判要复现"当时那次运行的口径"，
    #    不能拿命令行的默认值（默认 kb=senyu / mode=open）去覆盖 ——
    #    否则存出来的快照会被贴错标签（实测：fastapi 的那份被写成 senyu）。
    mode = str(payload.get("mode") or "open")
    args.kb = str(payload.get("kb") or args.kb)
    args.questions = questions_name

    results: list[AnswerResult] = []
    for row in payload.get("results", []):
        item = items.get(row["id"])
        if item is None:
            continue
        result = AnswerResult(
            id=row["id"],
            question=row["question"],
            refused=row.get("refused", False),
            llm_calls=row.get("llm_calls", 0),
            cited=row.get("cited", []),
            text=row.get("text", ""),
            error=row.get("error"),
        )
        answer_norm = _norm(_strip_citation_marks(result.text))
        for point in item.get("required_points", []):
            hit, weak = _point_hit(point, answer_norm)
            result.points.append((point, hit, weak))
        must_cite = item.get("must_cite", [])
        # ⚠️ 闭卷**不判引用**（它压根没检索），和正跑时的口径保持一致
        if must_cite and mode == "open":
            result.cite_ok = bool(set(must_cite) & set(result.cited))
        for banned in item.get("must_not_say", []):
            if _norm(banned) and _norm(banned) in answer_norm:
                result.forbidden_hit.append(banned)
        results.append(result)

    if not results:
        # ⚠️ **一条都没重判上就是失败**，不许打印一张 0/0 的报告然后存档 ——
        #    那看起来像"跑完了、没有题通过"，实际是"题号一个都没对上"。
        raise SystemExit(
            f"没有一条结果能重判：题号对不上（结果里是 {sorted({r['id'] for r in payload.get('results', [])})[:3]}…，"
            f"题集里是 {sorted(items)[:3]}…）"
        )

    print(f"重判 {path.name}（同一批回答、只换判分器）")
    _report_answer_layer(
        results,
        layer="generation",
        mode=mode,
        settings=get_settings(),
        args=args,
        started=datetime.now(tz=UTC),
    )
    return 0


def _modes(arg: str, *, layer: str) -> tuple[str, ...]:
    """这一层要跑哪几种模式。

    ⚠️ **拒答层只有开卷**：它的定义就是"检索到的证据够不够 → 要不要拒答"。
       闭卷没有候选、也没有分数，硬跑只会得到"全部拒答"，是假满分。
    """
    if layer == "refusal":
        if arg in {"closed", "random"}:
            raise SystemExit("拒答层只有开卷：闭卷 / 随机证据都没有「证据够不够」这回事")
        return ("open",)
    if arg == "both":
        return ("open", "closed")
    if arg == "all":
        # v1.4 B3：三组一起跑 —— 闭卷 → 随机证据 → 真证据（差值即归因）
        return ("closed", "random", "open")
    if arg == "random":
        return ("random",)
    return ("open",) if arg == "open" else ("closed",)


def _print_contrast(summaries: Sequence[dict[str, Any]], *, kb: str, layer: str) -> None:
    """打印 闭卷 / 随机证据 / 开卷 对照表 —— 这就是「对照实验」的结论页。"""
    print()
    print(f"===== 对照：{kb} · {layer} =====")
    print(
        f"{'模式':<8}{'要点全中':>10}{'要点命中率':>12}"
        f"{'含数字回答':>12}{'出现禁语':>10}{'引用对':>12}"
    )
    print("-" * 66)
    for row in summaries:
        # ⚠️ 只有开卷判引用：闭卷没检索，随机证据组的"引用"是随机的 —— 都不该显示成"0/20"
        cite = f"{row['cite_ok']}/{row['cite_checked']}" if row["cite_checked"] else "—（不判引用）"
        print(
            f"{row['mode']:<8}{row['all_points']:>6}/{row['total']:<3}"
            f"{row['points_rate']:>11.1%}"
            f"{row['answers_with_digits']:>8}/{row['total']:<3}"
            f"{row['forbidden_questions']:>7}/{row['total']:<2}"
            f"{cite:>12}"
        )
    by_mode = {row["mode"]: row for row in summaries}
    # ⚠️ 三组（v1.4 B3）时差值要**分两段算**，别再报"开卷 − 闭卷"那一个数 ——
    #    那一个数把"提示词的功劳"和"检索的功劳"混在一起，正是这个实验要拆开的东西。
    if {"closed", "random", "open"} <= set(by_mode):
        closed, random_row, opened = by_mode["closed"], by_mode["random"], by_mode["open"]
        prompt_gain = random_row["points_rate"] - closed["points_rate"]
        retrieval_gain = opened["points_rate"] - random_row["points_rate"]
        print(
            f"\n三组归因（要点命中率）：闭卷 {closed['points_rate']:.1%} → "
            f"随机证据 {random_row['points_rate']:.1%} → 开卷 {opened['points_rate']:.1%}"
        )
        print(f"  · 提示词 ＋ 格式的功劳 ＝ {prompt_gain:+.1%}（随机组 − 闭卷；**可能是负的**）")
        print(f"  · 检索的功劳 ＝ {retrieval_gain:+.1%}（开卷 − 随机组）")
        if prompt_gain < 0:
            print(
                "  ⚠️ 提示词这一段是**负贡献**：无关证据比没有证据更糟 —— "
                "模型会把不相关的段落当成依据，还更敢给具体数字。"
            )
    elif {"open", "closed"} <= set(by_mode):
        opened, closed = by_mode["open"], by_mode["closed"]
        delta = opened["all_points_rate"] - closed["all_points_rate"]
        print(
            f"\n要点全中率：开卷 {opened['all_points_rate']:.1%} − "
            f"闭卷 {closed['all_points_rate']:.1%} = **＋{delta:.1%}**"
        )
        # ⚠️ 这一行才是"幻觉"的证据：闭卷**没有回避**（照样给出具体数字），
        #    但那些数字是编的 —— 所以它同时"很自信"且"要点命中率很低"。
        print(
            f"含数字的回答：闭卷 {closed['answers_with_digits']}/{closed['total']} · "
            f"开卷 {opened['answers_with_digits']}/{opened['total']}"
            "　⟹ 闭卷不是「说不出」，是**自信地编**"
        )
        print(
            f"出现禁语的题数：闭卷 {closed['forbidden_questions']} → "
            f"开卷 {opened['forbidden_questions']}"
        )
    print()


def _dump_contrast(
    summaries: Sequence[dict[str, Any]], *, kb: str, layer: str, started: datetime
) -> Path:
    """把两组的汇总数写成一个文件（逐题回答仍在各自的 `{layer}-{mode}-*.json` 里）。"""
    out = RESULTS_DIR / f"contrast-{kb}-{layer}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": f"{layer}-contrast",
        "kb": kb,
        "generated_at": started.strftime("%Y%m%dT%H%M%SZ"),
        "note": (
            "闭卷 = 关掉检索、只让模型凭自身知识回答（app/rag/pipeline.py 的 closed_book）；"
            "开卷 = 线上的完整链路。两组用同一批题、同一个模型、同一个判分器。"
        ),
        "rows": list(summaries),
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return out


async def _run_answer_layers(args: argparse.Namespace) -> int:
    """按 `--layer` / `--mode` 跑，每次跑完立刻落盘。

    `--mode both` 时依次跑开卷、闭卷，再打一张对照表。
    ⚠️ 顺序固定为**先开卷后闭卷**：开卷是线上真实链路，先拿到的是"真实表现"，
       闭卷是对照；顺序反过来容易在出异常时把对照当成主结果。
    """
    layers = ("generation", "refusal") if args.layer == "all" else (args.layer,)
    for layer in layers:
        stamp = datetime.now(tz=UTC)
        summaries = [
            _report_answer_layer(
                await _run_answer_layer(args, layer=layer, mode=mode),
                layer=layer,
                mode=mode,
                settings=get_settings(),
                args=args,
                started=stamp,
            )
            for mode in _modes(args.mode, layer=layer)
        ]
        if len(summaries) > 1:
            _print_contrast(summaries, kb=args.kb, layer=layer)
            out = _dump_contrast(summaries, kb=args.kb, layer=layer, started=stamp)
            print(f"对照汇总已存：{out.relative_to(REPO_ROOT)}")
    return 0


def _skeleton(text: str) -> str:
    """把一段文字压成"骨架"：去掉空白与 `¥ , . ：`，转小写。

    ⚠️ 为什么不能按原串比：模型把 `¥1,280.00` 写成 `¥1280.00` 是**对的**，
        按原串比会把正确答案判成读错。骨架比的是"数字与字母序列对不对"。
    """
    return re.sub(r"[\s¥￥,，.。:：]", "", text or "").lower()


def _load_image_cases() -> list[dict[str, Any]]:
    path = IMAGES_META
    if not path.exists():
        raise SystemExit(f"缺真值文件：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{path} 的顶层结构不是列表")
    return [dict(item) for item in raw]


async def _read_image_cases(client: Any, cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """① 读图这一层：**只看"模型读到了什么"，不涉及检索与生成**。

    ⚠️ 与答案层**分开报**：答错了要知道是"读错了"还是"检索/生成错了" ——
        混成一个数字，badcase 就归因不了（这正是本项目分层评测的初衷）。
    """
    rows: list[dict[str, Any]] = []
    for case in cases:
        path = IMAGES_DIR / str(case["file"])
        if not path.exists():
            raise SystemExit(f"缺图：{path}（fixtures 没齐，先补图；**不许按 0 分算**）")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        try:
            analysis = await client.analyze(path.read_bytes(), content_type=mime)
        except ModelAPIError as exc:
            rows.append({**case, "error": f"ModelAPIError: {exc}"})
            continue

        skeleton = _skeleton(analysis.transcription)
        must_have = [str(token) for token in case.get("must_have") or []]
        missing = [token for token in must_have if _skeleton(token) not in skeleton]
        scene_ok: bool | None = None
        if case.get("scene_any"):
            scene = analysis.scene or ""
            scene_ok = any(str(key) in scene for key in case["scene_any"])
        exact_ok: bool | None = None
        if "has_exact_fields" in case:
            exact_ok = analysis.has_exact_fields == bool(case["has_exact_fields"])

        rows.append(
            {
                **case,
                "image_type": analysis.image_type,
                "scene": analysis.scene,
                "transcription": analysis.transcription,
                "fallback": analysis.fallback,
                "missing": missing,
                "scene_ok": scene_ok,
                "exact_ok": exact_ok,
                "ok": not missing and scene_ok is not False,
                #: ⚠️ `has_exact_fields` **不算硬断言**：它在代码里不控制任何分支
                #:    （抽单号是无条件的，见 `app/api/chat.py::_order_context`），
                #:    判错只意味着"多查或少查一次库"，对答案没有影响。
                #:    混进硬断言会把它算成失败，反倒掩盖真正的读错。
            }
        )
    return rows


def _print_image_reading(rows: Sequence[dict[str, Any]]) -> None:
    print()
    print("【读图层】模型从图里读到了什么（与答案层分开报）")
    print(f"{'文件':<22}{'类型':<12}{'必须字符串':>10}{'场景':>6}{'精确字段':>10}{'降级':>6}")
    print("-" * 72)
    for row in rows:
        if row.get("error"):
            print(f"{row['file']:<22} 出错：{row['error']}")
            continue
        total = len(row.get("must_have") or [])
        hit = total - len(row["missing"])
        scene = "—" if row["scene_ok"] is None else ("✓" if row["scene_ok"] else "✗")
        exact = "—" if row["exact_ok"] is None else ("✓" if row["exact_ok"] else "✗")
        print(
            f"{row['file']:<22}{row['image_type']:<12}{f'{hit}/{total}':>10}"
            f"{scene:>6}{exact:>10}{'是' if row['fallback'] else '否':>6}"
        )
        if row["missing"]:
            print(f"    缺：{' / '.join(row['missing'])}")
            print(f"    实际转写：{(row['transcription'] or '（空）')[:120]}")
    ok = sum(1 for row in rows if row.get("ok"))
    exact_miss = [str(row["file"]) for row in rows if row.get("exact_ok") is False]
    print(f"\n读图硬断言：**{ok}/{len(rows)} 张通过**（必须字符串全中 ＋ 场景命中）")
    if exact_miss:
        print(
            f"  ⚠️ `has_exact_fields` 与判据不符 {len(exact_miss)} 张："
            f"{', '.join(exact_miss)}（只影响要不要查一次订单库，不影响答案）"
        )


async def _run_image_answers(
    args: argparse.Namespace, rows: Sequence[dict[str, Any]], *, mode: str
) -> dict[str, Any]:
    """② 答案层：同一批问题，**带图 / 不带图**各答一遍。

    ⚠️ 有图的这一组用的是**真实读图结果**（上面刚跑出来的），不是真值 ——
        这样"图读错了"会如实反映到答案上，而不是被真值掩盖。
    """
    settings = get_settings()
    chat, embedder, reranker = build_clients(settings)
    cases = [row for row in rows if row.get("question")]
    factory = get_session_factory()
    results: list[dict[str, Any]] = []
    started = datetime.now(tz=UTC)

    async with factory() as session:
        tenant_id = await _resolve_tenant_id(session, settings.default_tenant_id)
        paths = await _document_paths(session, tenant_id, kb=args.kb)
        for row in cases:
            use_image = mode == "image"
            pipeline = RagPipeline(
                session=session,
                tenant_id=tenant_id,
                chat=chat,
                embedder=embedder,
                reranker=reranker,
                settings=settings,
                image_scene=(row.get("scene") or "") if use_image else "",
                image_text=(row.get("transcription") or "") if use_image else "",
                # 冻结时间基准：图片层的题不涉及版本，但两个 runner **同一套旋钮**，
                # 免得"这个参数在另一层不起作用"这种坑再来一次。
                ref_time=args.ref_time,
            )
            text, refused, error = "", False, None
            try:
                final: Answer | None = None
                async for event in pipeline.answer(question=str(row["question"])):
                    if isinstance(event, Answer):
                        final = event
                if final is None:
                    error = "没有拿到最终 Answer"
                else:
                    text, refused = final.text, final.refused
            except ModelAPIError as exc:
                error = f"ModelAPIError: {exc}"

            answer_norm = _norm(_strip_citation_marks(text))
            points = [
                (point, *_point_hit(point, answer_norm))
                for point in row.get("required_points") or []
            ]
            hit = sum(1 for _, ok, _ in points if ok)
            results.append(
                {
                    "id": row["file"],
                    "mode": mode,
                    "question": row["question"],
                    "refused": refused,
                    "text": text,
                    "points_hit": hit,
                    "points_total": len(points),
                    "ok": (not error) and hit == len(points),
                    "cited": [
                        paths.path_of(c.document_id) for c in (final.citations if final else [])
                    ],
                    **({"error": error} if error else {}),
                }
            )

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"image-{mode}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"kind": "image-answer", "mode": mode, "rows": results}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "mode": mode,
        "total": len(results),
        "ok": sum(1 for item in results if item["ok"]),
        "rows": results,
        "result_file": str(out.relative_to(REPO_ROOT)),
    }


async def _run_image(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = build_image_client(settings)
    if client is None:
        raise SystemExit("读图没启用：检查 IMAGE_ANALYSIS_ENABLED / API key")
    cases = _load_image_cases()
    if args.limit:
        cases = cases[: args.limit]

    print(f"图片 fixture：{len(cases)} 张 · 模型 {client.model}")
    rows = await _read_image_cases(client, cases)
    _print_image_reading(rows)

    if args.mode in ("image", "no-image", "both"):
        modes: tuple[str, ...] = ("image", "no-image") if args.mode == "both" else (args.mode,)
    else:
        # `--layer image` 没显式给 `--mode`（默认 open）时，按"带图 vs 不带图"两组跑
        modes = ("image", "no-image")
        summaries = [await _run_image_answers(args, rows, mode=mode) for mode in modes]
        print()
        print("【答案层】同一批问题：**带图** vs **不带图**")
        print(f"{'模式':<10}{'要点全中':>10}{'题数':>6}{'拒答':>6}")
        print("-" * 34)
        for summary in summaries:
            refused = sum(1 for item in summary["rows"] if item["refused"])
            print(
                f"{summary['mode']:<10}{summary['ok']:>6}/{summary['total']:<3}"
                f"{summary['total']:>6}{refused:>6}"
            )
        for summary in summaries:
            for item in summary["rows"]:
                mark = "✓" if item["ok"] else "✗"
                print(
                    f"  [{summary['mode']}] {mark} {item['id']}"
                    f"  要点 {item['points_hit']}/{item['points_total']}"
                    f"{'  （拒答）' if item['refused'] else ''}"
                )
        out = RESULTS_DIR / f"image-contrast-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        out.write_text(
            json.dumps(
                {
                    "kind": "image-contrast",
                    "reading": rows,
                    "answers": summaries,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n对照已存：{out.relative_to(REPO_ROOT)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="grounded-qa 评测 runner")
    parser.add_argument(
        "--layer",
        default="retrieval",
        choices=["retrieval", "generation", "refusal", "image", "all"],
        help="哪一层（all 只跑 generation + refusal；retrieval / image 单独跑）",
    )
    parser.add_argument("--kb", default="senyu", help="知识库前缀（ingest 时 --kb 的值）")
    parser.add_argument(
        "--mode",
        default="open",
        choices=["open", "closed", "random", "all", "image", "no-image", "both"],
        help=(
            "generation/refusal 层：open=线上链路（检索＋精排＋拒答）；"
            "closed=闭卷对照（关掉检索）；random=随机证据对照（开卷提示词＋无关段落）；"
            "both=开卷＋闭卷；all=闭卷→随机证据→开卷（三组归因，见 v1.4 B3）。"
            "image 层：image=带图、no-image=不带图、both=两者对照；"
            "默认（open）在 image 层等价于 both。"
        ),
    )
    parser.add_argument(
        "--seed",
        default="20261023",
        help="随机证据组的种子（固定住，否则「某次运气好」无法复现）",
    )
    parser.add_argument("--top-k", type=int, default=5, help="hit@k 的 k")
    parser.add_argument("--only", default="all", choices=["all", *ABLATIONS], help="只跑某一组消融")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（试水用）")
    parser.add_argument("--questions", default=None, help="换一个题集文件（默认 retrieval.yaml）")
    parser.add_argument(
        "--fulltext-min",
        type=float,
        default=None,
        help="覆盖 fulltext_min_similarity（做门槛实验用）",
    )
    parser.add_argument(
        "--show-answers", action="store_true", help="把每题的回答打出来（核对判分是否公允）"
    )
    parser.add_argument(
        "--rescore", default=None, help="用已存的结果文件重算判分（不调模型，只换尺子）"
    )
    parser.add_argument(
        "--scope",
        default=None,
        choices=["all", "current"],
        help=(
            "召回范围（v1.3）：current = 只在有资格被引用的文档里召回（产品态）；"
            "all = 全库召回，用于复现历史数字。两支尺子并存，**都不是作弊**"
        ),
    )
    parser.add_argument(
        "--ref-time",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "冻结参照时刻（v1.3 阶段 B2）。不传 = 用今天。"
            "历史版本只在它当时有效 —— 要测'2024 年买的沙发'这类问题，"
            "就得把时间钉死，否则同一批题换个日子跑就换了答案"
        ),
    )
    args = parser.parse_args(argv)
    if args.ref_time:
        # ⚠️ 转成 `date` 存回 namespace：下游（retrieval / pipeline）只认 `date`，
        #    解析只在这里做一次，避免每个调用点各写一遍字符串解析。
        try:
            args.ref_time = date.fromisoformat(args.ref_time)
        except ValueError:
            raise SystemExit(f"--ref-time 要 YYYY-MM-DD 形式，收到 {args.ref_time!r}") from None
    if args.scope:
        # ⚠️ 必须在第一次 `get_settings()` 之前设进环境；而且**光设环境不够** ——
        #    `get_settings` 是 `lru_cache` 的，模块导入阶段可能已经缓存过默认值，
        #    所以这里必须 `cache_clear()`，否则 `--scope` **静默失效**（跑出来两组数字一样，
        #    看起来像"治理没效果"，其实是参数没进去 —— 2026-10-22 实测踩中）。
        os.environ["RETRIEVAL_SCOPE"] = args.scope
        get_settings.cache_clear()
    if args.rescore:
        return _rescore_generation(args)
    if args.layer == "retrieval":
        return asyncio.run(_run_retrieval(args))
    if args.layer == "image":
        return asyncio.run(_run_image(args))
    return asyncio.run(_run_answer_layers(args))


if __name__ == "__main__":
    raise SystemExit(main())
