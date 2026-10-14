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

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.rag import prompt as prompt_module
from app.rag.providers import ChatClient, EmbeddingClient, ModelAPIError, RerankClient
from app.rag.retrieval import HybridRetriever, RetrievedChunk


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

    def as_dict(self) -> dict[str, object]:
        """转成可直接入库（`messages.citations`）的结构。"""
        return {
            "index": self.index,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "score": round(self.score, 4),
            "score_kind": self.score_kind,
            "snippet": self.snippet,
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
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._chat = chat
        self._embedder = embedder
        self._reranker = reranker
        self._settings = settings or get_settings()

    # ---------- 检索 ----------

    async def _retrieve(self, question: str) -> tuple[list[RetrievedChunk], float | None, str]:
        """混合检索 + 精排。

        Returns:
            `(最终候选, 置信度, 置信度的口径)`。口径取值：`rerank` / `vector_cosine` / `none`。
        """
        settings = self._settings
        retriever = HybridRetriever(self._session, tenant_id=self._tenant_id)

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

        hits, confidence, score_kind = await self._retrieve(question)
        hits = hits[: settings.rerank_top_n]

        refused = self._should_refuse(hits, confidence)
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
                citations=[],
                confidence=confidence,
                latency_ms=_elapsed_ms(started),
                extra={"reason": "no_evidence" if not hits else "low_confidence"},
            )
            return

        messages = prompt_module.build_messages(question=question, hits=hits, history=history)
        collected: list[str] = []
        error: str | None = None

        try:
            async for piece in self._chat.stream(messages):
                collected.append(piece)
                yield Delta(text=piece)
        except ModelAPIError as exc:
            # ⚠️ **不要把已经拿到的部分丢掉**：用户看到了半截回答，
            #    如果库里存的是空，事后排查就对不上了。
            error = str(exc)

        yield Answer(
            text="".join(collected),
            refused=False,
            citations=citations,
            confidence=confidence,
            latency_ms=_elapsed_ms(started),
            error=error,
            extra={"score_kind": score_kind},
        )

    def _should_refuse(self, hits: Sequence[RetrievedChunk], confidence: float | None) -> bool:
        """判拒答。**这是幻觉治理最关键的一行判断。**

        ⚠️ 三种情况分开看，不要混成一句 `confidence < threshold`：

        | 情况 | 结论 | 为什么 |
        |---|---|---|
        | 一条都没检索到 | **拒答** | 没有任何证据，答什么都是编 |
        | 有 rerank 分数 | 按 `answer_min_score` 判 | 交叉编码的分数是**校准过的相关性**，可以定阈值 |
        | 没有 rerank 分数 | **不拒答** | 只有 trgm 相似度时，那个数字**不是相关性语义**，拿它比阈值等于瞎判 |
        """
        if not hits:
            return True
        if confidence is None:
            # 纯全文命中（没有 embedding / 没开 rerank）→ 没有可信的置信度。
            # 宁可答（并在引用里给出处让人核对），也不要用一个无意义的数字假装判断过。
            return False
        return confidence < self._settings.answer_min_score


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
