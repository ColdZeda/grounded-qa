"""RAG 主流程：**检索 → 精排 → 判拒答 → 生成**。

⚠️ **顺序不能换，尤其是"判拒答"必须在"生成"之前。**
    让模型自己决定"要不要拒答"是不可靠的 —— 它天然倾向于给出一个听起来合理的答案。
    拒答必须是一段**确定性代码**的结论：

        证据不够 → 直接返回固定文案，**根本不调模型**

    这样做有三个好处：
      1. 拒答率成了一个**真实、可优化**的指标（而不是模型心情的副产品）
      2. 拒答时**不花 token**（成本可预期）
      3. 拒答文案**不会被模型"润色"成一句看似有依据的话**

⚠️ **置信度必须来自 rerank 分数，不能来自 RRF 分数。**
    RRF 是名次的函数（`1/(k+rank)`），库里只有一条毫不相关的内容时，它照样排第一、
    照样拿到 0.016。拿它比阈值 = 永远不拒答。
    所以：开 rerank → 用 rerank 分数；关 rerank → 退而用**向量余弦相似度**（有语义）；
    两条路都没有（纯全文命中）→ **没有可用的置信度，只能不拒答**（并在事件里标注）。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.metrics import record_citation_support
from app.rag import citations as citation_module
from app.rag import followup as followup_module
from app.rag import prompt as prompt_module
from app.rag.providers import ChatClient, EmbeddingClient, ModelAPIError, RerankClient
from app.rag.reftime import RefTime, RefTimeSource, resolve_ref_time
from app.rag.retrieval import HybridRetriever, RetrievedChunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Citation:
    """一条引用。**`index` 必须与提示词里的编号一致**（模型回答中的 `[2]` 指向它）。"""

    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    snippet: str
    #: 这个分数是**什么口径**：`rerank` / `vector_cosine` / `fulltext_trgm`。
    #: ⚠️ 必须标出来：三种分数**量纲不同**，界面上写一个笼统的"相关度 0.62"
    #:    会让人以为它们可比 —— 实际上 0.62 的 rerank 分和 0.62 的余弦相似度
    #:    完全是两回事。
    score_kind: str
    #: 这块在文档里的序号（「看原文」靠它定位：`GET /documents/{id}/chunks?around=`）。
    #: ⚠️ 默认 0 只是为了老数据反序列化不炸；新回答一定带上真实序号。
    ordinal: int = 0

    def as_dict(self) -> dict[str, object]:
        """转成可直接入库（`messages.citations`）的结构。"""
        return {
            "index": self.index,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "score": round(self.score, 4),
            "score_kind": self.score_kind,
            "snippet": self.snippet,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class Sources:
    """检索完成、开始生成之前发出的第一个事件 —— 让前端先把"参考来源"渲染出来。"""

    citations: list[Citation]
    confidence: float | None
    refused: bool


@dataclass(frozen=True)
class Delta:
    """回答的一个增量片段。"""

    text: str


@dataclass(frozen=True)
class Answer:
    """生成结束（或直接拒答）后的最终结果，由调用方负责落库。"""

    text: str
    refused: bool
    citations: list[Citation]
    confidence: float | None
    latency_ms: int
    #: 非空表示生成过程中模型 API 出错 —— 此时 `text` 是**已经拿到的部分**，不要丢。
    error: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
    #: 这次回答**实际花了多少 token**（模型 API 回报的 `usage`）。
    #: ⚠️ 拒答时是空的 —— 拒答**根本没调模型**，所以一分钱没花。
    #:    这正是"拒答要在调模型之前判"（约定 17）的另一个好处：账面上看得出来。
    usage: dict[str, int] = field(default_factory=dict)


RagEvent = Sources | Delta | Answer


def build_clients(
    settings: Settings | None = None,
) -> tuple[ChatClient, EmbeddingClient | None, RerankClient | None]:
    """按配置造三个客户端。

    ⚠️ **没有 key 就返回 `None`，而不是造一个注定失败的客户端。**
        这让"embedding 还没配"退化成"退化为纯全文检索"，而不是整个问答挂掉 ——
        服务能用比功能齐全重要。
    """
    settings = settings or get_settings()

    chat = ChatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        # token 计量靠这个（`app/core/limits.py` 的配额就是拿它的数记账）
        include_usage=settings.llm_stream_usage,
    )

    embedder: EmbeddingClient | None = None
    if settings.embedding_api_key:
        embedder = EmbeddingClient(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )

    reranker: RerankClient | None = None
    if settings.rerank_enabled and settings.rerank_api_key:
        reranker = RerankClient(
            base_url=settings.rerank_base_url,
            api_key=settings.rerank_api_key,
            model=settings.rerank_model,
        )

    return chat, embedder, reranker


class RagPipeline:
    """一次问答的完整流程。**一个实例只服务一个问题。**"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        chat: ChatClient,
        embedder: EmbeddingClient | None = None,
        reranker: RerankClient | None = None,
        settings: Settings | None = None,
        closed_book: bool = False,
        image_scene: str = "",
        image_text: str = "",
        order_facts: str = "",
        order_placed_at: Sequence[datetime | date] = (),
        ref_time: date | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._chat = chat
        self._embedder = embedder
        self._reranker = reranker
        self._settings = settings or get_settings()
        #: 这条提问带的那张图读到了什么（**已经由调用方读好**）。
        #: ⚠️ 检索只用 `image_scene`，**不用 `image_text`**：
        #:    转写里通常有单号/金额这类精确串，它们对 trgm 和向量都没有语义，
        #:    塞进 query 只会把召回搅乱（`PLAN §8.3` D5 的"三不"）。
        #:    `image_text` 只进生成上下文，而且明确标了"可能有误"。
        self._image_scene = image_scene
        self._image_text = image_text
        #: 【订单信息】那段文字（`app/orders.py::render_order_facts` 渲染好）。
        #: ⚠️ 空串 = 没查到订单。**故意不给"没查到"这种句子** ——
        #:    给了模型会顺着猜，不给它就没有材料可编。
        self._order_facts = order_facts
        #: 上述订单的**下单时间**（v1.3 B2）。用来定 `ref_time` —— 老订单要以
        #: 当年生效的条款为准，见 `app/rag/reftime.py`。
        #: ⚠️ 订单由**调用方**查（要 `user_id`，pipeline 只有 `tenant_id`），这里只收结果。
        self._order_placed_at = tuple(order_placed_at)
        #: **冻结**的参照时刻（`evals/run.py --ref-time`）。非空则不再看订单、也不读问句。
        #: ⚠️ 只给评测用：同一批题在不同日子跑必须能复现。线上永远是 `None`。
        self._ref_time = ref_time
        #: **闭卷**：完全跳过检索，直接让模型凭自身知识回答。
        #: ⚠️ 只给评测的对照组用（`evals\run.py --mode closed`），**线上永远是 `False`** ——
        #:    这个开关存在的意义是让"有证据"与"没证据"跑**同一段代码**，
        #:    否则对照组和被试组的差异里会混进"两套实现"的噪音。
        #:    为什么闭卷不能同时保留拒答：拒答阈值挂在检索分数上，没有检索就没有分数，
        #:    "一律拒答"会让对照组恒为 0 分 —— 那不是基线，那是不及格。
        self._closed_book = closed_book

    # ---------- 检索 ----------

    def _resolve_ref_time(self, question: str) -> RefTime:
        """这次问答**用哪一天的眼光**看知识库（v1.3 B2）。

        ⚠️ 冻结值优先于一切：评测里"同一批题、同一天跑"是复现的前提。
        ⚠️ 注意传的是**用户原话**，不是拼了图片描述的检索 query ——
           图片描述里不该出现日期语义，真出现了也不该被当成用户的时间基准。
        """
        if self._ref_time is not None:
            return RefTime(self._ref_time, RefTimeSource.FROZEN)
        return resolve_ref_time(question=question, order_placed_at=self._order_placed_at)

    async def _retrieve(
        self, question: str, *, ref_time: date | None = None
    ) -> tuple[list[RetrievedChunk], float | None, str]:
        """混合检索 + 精排。

        Args:
            ref_time: **有效性**判定用的参照时刻（"这份文档当时有效吗"）。
                不影响任何分数，只影响哪些文档有资格进候选。

        Returns:
            `(最终候选, 置信度, 置信度的口径)`。口径取值：`rerank` / `vector_cosine` / `none`。
        """
        settings = self._settings
        # ⚠️ `scope` 决定"**有没有资格被引用**"，与"相不相关"无关（v1.3 阶段 B）；
        #    `ref_time` 决定"**在参照时刻**有没有资格"（v1.3 阶段 B2）。
        retriever = HybridRetriever(
            self._session,
            tenant_id=self._tenant_id,
            scope=settings.retrieval_scope,
            ref_time=ref_time,
        )

        query_embedding: list[float] | None = None
        if self._embedder is not None:
            try:
                vectors = await self._embedder.embed([question])
                query_embedding = vectors[0] if vectors else None
            except ModelAPIError:
                # ⚠️ **embedding 挂了不该让问答挂掉**：退化成纯全文检索，
                #    照样能答（只是召回差一些）。可用性 > 功能齐全。
                query_embedding = None

        hits, vector_scores = await retriever.search(
            query=question,
            query_embedding=query_embedding,
            top_k=settings.retrieve_top_k,
            rrf_k=settings.rrf_k,
            fulltext_min_similarity=settings.fulltext_min_similarity,
            # 元数据锁：向量检索只比"同一个模型"生成的块（见 `embedding_lock`）
            embedding_model=self._embedder.model if self._embedder is not None else None,
        )
        if not hits:
            return [], None, "none"

        if self._reranker is not None:
            ranked: list[tuple[int, float]] = []
            try:
                ranked = await self._reranker.rerank(
                    question, [hit.content for hit in hits], top_n=settings.rerank_top_n
                )
            except ModelAPIError:
                # ⚠️ 同上：精排是**锦上添花**，不是必需品。挂了就退回 RRF 顺序。
                ranked = []
            if ranked:
                # ⚠️ RRF 的顺序在这里被**完全丢弃** —— 这正是精排的意义：
                #    交叉编码看的是 (query, doc) 配对后的相关性，比 RRF 的"名次投票"更准。
                scores = {
                    hits[index].chunk_id: score for index, score in ranked if 0 <= index < len(hits)
                }
                reranked = [
                    RetrievedChunk(
                        chunk_id=hits[index].chunk_id,
                        document_id=hits[index].document_id,
                        ordinal=hits[index].ordinal,
                        content=hits[index].content,
                        score=score,
                    )
                    for index, score in ranked
                    if 0 <= index < len(hits)
                ]
                if reranked:
                    return reranked, max(scores.values()), "rerank"

        # 没走 rerank：置信度只能退而用**向量余弦相似度**（它有"相似"的语义）。
        if vector_scores:
            best = max(vector_scores[hit.chunk_id] for hit in hits if hit.chunk_id in vector_scores)
            return hits, best, "vector_cosine"

        # 只有全文命中：trgm 分数不是相关性语义，**没有可信的置信度**
        return hits, None, "none"

    # ---------- 主流程 ----------

    async def answer(
        self,
        *,
        question: str,
        history: Sequence[dict[str, str]] = (),
    ) -> AsyncIterator[RagEvent]:
        """跑完整条链，逐个 yield 事件。

        ⚠️ 这是 **async generator**：`Sources` 一定先于任何 `Delta` 发出，
            这样前端能先画出"参考来源"，而不是等回答出来才补。
        """
        started = time.perf_counter()
        settings = self._settings
        # ⚠️ **在检索之前定时间基准**（v1.3 B2）：它决定"哪些文档有资格进候选"。
        #    放到检索之后就没有意义了 —— 那时 top-k 已经定死。
        ref_time = self._resolve_ref_time(question)

        #: 短追问拼出来的检索 query（空 = 没拼）。放进 `extra` 是为了 badcase 归因：
        #: "这题召回错了"要能一眼看出是不是改写惹的祸。
        followup_query = ""
        if self._closed_book:
            # ---- 闭卷：不检索、不判拒答、没有引用 ----
            hits: list[RetrievedChunk] = []
            confidence: float | None = None
            score_kind = "closed_book"
            citations: list[Citation] = []
            refused = False
        else:
            #: v1.4 C1'：**短追问**（「德国呢」）自己检索不到东西 —— 把上一轮问句拼进检索 query。
            #: ⚠️ 只影响检索：提示词里仍然是用户这一问的原话（不替用户重述问题）。
            #:    触发条件刻意保守（≤15 字 ＋ 指代词 ＋ 有历史），见 `app/rag/followup.py`。
            followup_query = followup_module.retrieval_query(question, history)
            #: ⚠️ 带图时检索 query = `scene` + 用户原话（`PLAN §8.3` D6）。
            #:    为什么不加 `image_text`：转写里是单号/金额这类**精确串**，
            #:    对全文(trgm)和向量都没有语义，加进去只会稀释原话的召回。
            #:    `scene` 是"图里有什么"的自然语言描述，正好补原话里缺失的语境
            #:    （用户只发一句"这能退吗"，光看这句话检索不到"破损"）。
            base_query = followup_query or question
            search_query = (
                f"{self._image_scene} {base_query}".strip() if self._image_scene else base_query
            )
            hits, confidence, score_kind = await self._retrieve(
                search_query, ref_time=ref_time.value
            )
            hits = hits[: settings.rerank_top_n]

            refused = self._should_refuse(hits, confidence, score_kind)
            # 引用卡片上的分数口径：`none` 只可能是"纯全文命中"，那就是 trgm 分数
            citation_kind = "fulltext_trgm" if score_kind == "none" else score_kind
            citations = [
                Citation(
                    index=index,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    score=hit.score,
                    snippet=prompt_module.make_snippet(hit.content),
                    score_kind=citation_kind,
                    ordinal=hit.ordinal,
                )
                for index, hit in enumerate(hits, start=1)
            ]

        yield Sources(citations=citations, confidence=confidence, refused=refused)

        if refused:
            # ⚠️ 到这里就**结束**，不调模型（见模块 docstring）
            for piece in _chunk_text(prompt_module.REFUSAL_ANSWER):
                yield Delta(text=piece)
            yield Answer(
                text=prompt_module.REFUSAL_ANSWER,
                refused=True,
                # ⚠️ **拒答时也带上 citations**（2026-10-15 定，见 `architecture.md` §9.19）。
                #    它们不是"引用"（并没有被引用），而是**"检索到了、但都不够好"的证据** ——
                #    这正是 badcase 归因的入口。丢掉它，就等于每次拒答都只说"我不知道"，
                #    却**不留下任何"我为什么不知道"**。
                #    ⚠️ 副作用：`Answer.citations` 从此混装两种含义（引用 / 候选），
                #       字段名是将就的，记在 `architecture.md` §10.3。
                citations=citations,
                confidence=confidence,
                latency_ms=_elapsed_ms(started),
                extra={
                    "reason": "no_evidence" if not hits else "low_confidence",
                    **ref_time.as_extra(),
                },
            )
            return

        messages = (
            prompt_module.build_closed_book_messages(question=question, history=history)
            if self._closed_book
            else prompt_module.build_messages(
                question=question,
                hits=hits,
                history=history,
                order_facts=self._order_facts,
                image_scene=self._image_scene,
                image_text=self._image_text,
                # 只有"真的收到了时间信号"才告诉模型 —— 平白说一句"按今天"会
                # 让模型以为用户在问时间，反而多一层猜测（见 `RefTime.informed`）。
                ref_time=ref_time if ref_time.informed else None,
            )
        )
        collected: list[str] = []
        error: str | None = None
        # ⚠️ usage 用**每次回答自己的**字典收集（不是客户端上的属性）：
        #    客户端是进程内单例，并发请求会互相覆盖。
        usage: dict[str, int] = {}

        try:
            async for piece in self._chat.stream(messages, usage=usage):
                collected.append(piece)
                yield Delta(text=piece)
        except ModelAPIError as exc:
            # ⚠️ **不要把已经拿到的部分丢掉**：用户看到了半截回答，
            #    如果库里存的是空，事后排查就对不上了。
            error = str(exc)

        # 引用可核对性（v1.3 收尾）：**流式发完了才能核**，所以放在这里。
        # ⚠️ 只记录 + 打 warning，**不改写已经发出去的文字**（理由见 `citations.py`）。
        audit = citation_module.audit("".join(collected), hits)
        if audit.checked:
            record_citation_support(audit.ok)
        if not audit.ok:
            logger.warning(
                "引用串号：%d/%d 句对不上（首个：markers=%s atoms=%s）",
                audit.unsupported,
                audit.checked,
                audit.issues[0].markers,
                audit.issues[0].atoms,
            )

        yield Answer(
            text="".join(collected),
            refused=False,
            citations=citations,
            confidence=confidence,
            latency_ms=_elapsed_ms(started),
            error=error,
            extra={
                "score_kind": score_kind,
                **ref_time.as_extra(),
                # 引用可核对性：**只抓不改**（理由见 `app/rag/citations.py`）
                "citation_audit": audit.summary(),
                # 追问改写（v1.4 C1'）：非空表示这次检索用了"上一轮问句 ＋ 本问"。
                # ⚠️ 记账是为了 badcase 归因 —— "召回错了"要能一眼看出是不是改写惹的祸。
                **({"followup_query": followup_query} if followup_query else {}),
            },
            usage=usage,
        )

    def _should_refuse(
        self,
        hits: Sequence[RetrievedChunk],
        confidence: float | None,
        score_kind: str = "none",
    ) -> bool:
        """判拒答。**这是幻觉治理最关键的一行判断。**

        ⚠️ 分**四种**情况看，不要混成一句 `confidence < threshold`：

        | 情况 | 结论 | 为什么 |
        |---|---|---|
        | 一条都没检索到 | **拒答** | 没有任何证据，答什么都是编 |
        | 有 **rerank** 分数 | 按 `answer_min_score`（0.60）判 | 交叉编码的分数是**校准过的相关性**，可以定阈值 |
        | 只有**向量余弦** | 按 `answer_min_cosine`（0.45）判 | ⚠️ **必须换一把尺子** —— 见下面那条 |
        | 只有 **trgm** 相似度 | **不拒答** | trgm 那个数字**不是相关性语义**，拿它比阈值等于瞎判 |

        ⚠️ **2026-10-17（D2 收口）：两套阈值，按 `score_kind` 分。**
            原来无论哪种分数都用 `answer_min_score`（0.60）。但两种分数**分布不同** ——
            B 库实测：可答 25 题余弦 0.409~0.688 ／ 不可答 13 题 0.352~0.578，
            **重叠区 0.409~0.578 几乎分不开**。拿 0.60 去比余弦会**误拒 19/25 个可答题**
            （接口照样 200，看起来像"知识库里确实没有" —— **静默的坏**）。
            换成 0.45 后：正确拒答 8/13、误拒可答 1/25（偏向"宁可放过"，因为误拒更伤体验）。
            ⟹ **rerank 关掉时拒答能力显著下降，这是降级，不是等价配置。**
            完整数据 → `docs\\architecture.md` §5.3 / §9.25，`..\\plan\\PROJECT-PLAN.md` §8.4 D2。

        ⚠️ **2026-10-16 更正**：这张表原先只有三行，把后两种合并成
            "没有 rerank 分数 → 不拒答" —— **那是错的**。
            判据是 `confidence is None`（只有 trgm 命中才是），而 `vector_cosine`
            分支**给得出 `confidence`**，于是同一个阈值会被拿去比余弦。
            合并那一行会让读者以为"rerank 一挂就不判了"，
            而真实行为是"**照样判，只是拿错了尺子**" —— 后者才是要知道的事。
        """
        if not hits:
            return True
        if confidence is None:
            # 纯全文命中（没有 embedding / 没开 rerank）→ 没有可信的置信度。
            # 宁可答（并在引用里给出处让人核对），也不要用一个无意义的数字假装判断过。
            return False
        # ⚠️ **不能合并成一个阈值**：`rerank` 分与余弦不是同一个量纲。
        threshold = (
            self._settings.answer_min_score
            if score_kind == "rerank"
            else self._settings.answer_min_cosine
        )
        return confidence < threshold


def _chunk_text(text: str, *, size: int = 24) -> list[str]:
    """把固定文案切成小块再流式发出。

    ⚠️ 拒答文案本来可以一次性发出，但**前端只实现了一种渲染路径**（逐段追加）。
        一次性发出要求前端额外处理"整段替换"，多一条分支就多一处不一致。
        统一走增量，前端只有一条路。
    """
    return [text[i : i + size] for i in range(0, len(text), size)]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "Answer",
    "Citation",
    "Delta",
    "RagEvent",
    "RagPipeline",
    "Sources",
    "build_clients",
]
