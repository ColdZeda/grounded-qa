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
import re
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
from app.rag.pipeline import Answer, RagPipeline, build_clients
from app.rag.providers import ChatClient, ModelAPIError, aclose_http_client
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


# ---------------------------------------------------------------- 生成层 / 拒答层

#: 判分时只认"数字"和"拉丁词"这两类原子 —— 它们是政策文档里**最不可替换**的部分。
#: （"7 天"和"30 天"是同义词吗？不是。而"退货"和"退换"在语义上等价，不该强判。）
_ATOM = re.compile(r"\d+(?:\.\d+)?%?|[A-Za-z]{2,}")


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

    1. 要点里的**数字/百分比/拉丁词**必须**全部**出现在回答里 —— 硬条件；
    2. 中文部分看**二元组覆盖率**：要点里的中文二元组有多少比例出现在回答里，
       阈值 0.6（去掉括号补充说明后再算）。

    ⚠️ "弱判定" = 要点里既没数字也没拉丁词，只靠二元组覆盖 ——
       这种判定天然偏松，输出里单独计数，别和数字型要点同等看待。
    """
    atoms = _ATOM.findall(point)
    if atoms:
        # 有数字/拉丁词 ⟹ **以它为准**。它们才是政策里不可替换的证据
        # （"7 天"和"30 天"不是同义词；而"新马"与"新加坡/马来西亚"是同义的 —— 后者不该扣分）。
        return all(_norm(atom) in answer_norm for atom in atoms), False

    plain = re.sub(r"[（(][^）)]*[）)]", "", point)
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
            for index, item in enumerate(items, start=1):
                spy.calls = 0
                pipeline = RagPipeline(
                    session=session,
                    tenant_id=tenant_id,
                    chat=spy,  # type: ignore[arg-type]
                    embedder=embedder,
                    reranker=reranker,
                    settings=settings,
                    # 闭卷：同一段代码，只是不检索（见 `RagPipeline` 的 `closed_book`）
                    closed_book=mode == "closed",
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
    tag = "开卷（有检索证据）" if mode == "open" else "闭卷（无检索，凭模型自身知识）"
    print(f"===== {args.kb} · {layer} · {tag} =====")
    if layer == "generation":
        ok = sum(1 for r in results if r.passed)
        strict = [r for r in results if r.weak_points == 0]
        strict_ok = sum(1 for r in strict if r.passed)
        rules = "要点全中 ＋ 无禁语" if mode == "closed" else "要点全中 ＋ 引用对 ＋ 无禁语"
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
    items = {item["id"]: item for item in _load_items(QUESTIONS_DIR / "generation.yaml")}

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
        if must_cite:
            result.cite_ok = bool(set(must_cite) & set(result.cited))
        for banned in item.get("must_not_say", []):
            if _norm(banned) and _norm(banned) in answer_norm:
                result.forbidden_hit.append(banned)
        results.append(result)

    print(f"重判 {path.name}（同一批回答、只换判分器）")
    _report_answer_layer(
        results,
        layer="generation",
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
        if arg == "closed":
            raise SystemExit("拒答层只有开卷：闭卷没有检索分数，无从判拒答")
        return ("open",)
    if arg == "both":
        return ("open", "closed")
    return ("open",) if arg == "open" else ("closed",)


def _print_contrast(summaries: Sequence[dict[str, Any]], *, kb: str, layer: str) -> None:
    """打印闭卷 / 开卷对照表 —— 这就是「对照实验」的结论页。"""
    print()
    print(f"===== 对照：{kb} · {layer} =====")
    print(
        f"{'模式':<8}{'要点全中':>10}{'要点命中率':>12}"
        f"{'含数字回答':>12}{'出现禁语':>10}{'引用对':>12}"
    )
    print("-" * 66)
    for row in summaries:
        cite = (
            f"{row['cite_ok']}/{row['cite_checked']}" if row["cite_checked"] else "—（闭卷无引用）"
        )
        print(
            f"{row['mode']:<8}{row['all_points']:>6}/{row['total']:<3}"
            f"{row['points_rate']:>11.1%}"
            f"{row['answers_with_digits']:>8}/{row['total']:<3}"
            f"{row['forbidden_questions']:>7}/{row['total']:<2}"
            f"{cite:>12}"
        )
    by_mode = {row["mode"]: row for row in summaries}
    if {"open", "closed"} <= set(by_mode):
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="grounded-qa 评测 runner")
    parser.add_argument(
        "--layer",
        default="retrieval",
        choices=["retrieval", "generation", "refusal", "all"],
        help="哪一层（all 只跑 generation + refusal；retrieval 单独跑）",
    )
    parser.add_argument("--kb", default="senyu", help="知识库前缀（ingest 时 --kb 的值）")
    parser.add_argument(
        "--mode",
        default="open",
        choices=["open", "closed", "both"],
        help=(
            "open=线上链路（检索＋精排＋拒答）；"
            "closed=闭卷对照（关掉检索，模型凭自身知识答）；"
            "both=两组都跑并打一张对照表"
        ),
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
    args = parser.parse_args(argv)
    if args.rescore:
        return _rescore_generation(args)
    if args.layer == "retrieval":
        return asyncio.run(_run_retrieval(args))
    return asyncio.run(_run_answer_layers(args))


if __name__ == "__main__":
    raise SystemExit(main())
