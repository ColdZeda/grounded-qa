"""检索：**全文 + 向量 → RRF 融合**，再交给 rerank 精排。

⚠️ **为什么不是"向量检索就够了"**：
    向量检索擅长**语义相近**（"怎么重置密码" 命中 "密码找回流程"），
    但对**精确串、型号、错误码、专有名词**很弱 ——
    而客服场景里恰恰大量是后者（"报错 E1024"、"X200 的保修期"）。
    全文检索正好补这一块。两条路**失败模式不同**，融合才有意义。

⚠️ **RRF（Reciprocal Rank Fusion）只用"名次"，不用"分数"。**
    这是它最大的优点：向量余弦相似度（0~1）和 trgm 相似度（0~1）**量纲根本不同**，
    直接加权求和等于随机给权重。RRF 把它们变成同一个尺度：`1/(k + rank)`。

⚠️ **但 RRF 分数不能用来做"置信度拒答"** —— 它是名次的函数，不是相似度的函数：
    就算库里只有一条毫不相关的内容，它照样排第一、照样拿到 `1/(60+1)`。
    所以拒答阈值必须挂在 **rerank 分数**（或退而求其次，向量余弦）上。见 `pipeline.py`。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk
from app.db.repository import TenantScopedRepository


@dataclass(frozen=True)
class RetrievedChunk:
    """一路检索命中的一条 chunk。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int
    content: str
    #: 该路自己的分数（向量余弦相似度 / trgm 相似度）。**跨路不可比**。
    score: float


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]],
    *,
    k: int = 60,
) -> dict[uuid.UUID, float]:
    """RRF 融合：把多路**排名**合成一个分数。

    公式：`score(d) = Σ 1 / (k + rank_i(d))`，`rank` 从 1 开始。

    ⚠️ `k=60` 不是拍脑袋：它来自原论文（Cormack et al. 2009），作用是**压平头部**——
        `k` 很小时，第 1 名和第 2 名的差距极大（1/1 vs 1/2），融合几乎退化成"看第一路"；
        `k=60` 让前几名的差距变小，多路的一致性才真正起作用。
        这个值是**配置项**（`RRF_K`），换数据集应该重新调。

    ⚠️ 只出现在某一路里的文档**照样得分**（而不是 0）。这是有意的：
        "向量路第 3 名"本身就是一个强信号，不该因为全文路没召回它就归零。

    Args:
        rankings: 多路的**有序** id 列表。顺序 = 名次。
        k: RRF 的平滑常数。

    Returns:
        `{id: 融合分数}`。分数不可跨查询比较（依赖 `k` 与路数）。
    """
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


class HybridRetriever:
    """在本租户范围内做混合检索。

    ⚠️ 所有查询都建立在 `TenantScopedRepository.select()` 之上 ——
        它已经把 `WHERE tenant_id = ?` 焊进去了。**不要绕开它自己写 select**。
    """

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._repo = TenantScopedRepository(session, tenant_id=tenant_id, model=Chunk)

    async def _fulltext(
        self, query: str, *, limit: int, min_similarity: float
    ) -> list[RetrievedChunk]:
        """全文路：`pg_trgm` 的 `word_similarity`。

        ⚠️ **为什么是 `word_similarity` 而不是 `similarity`**：
            `similarity(a, b)` 是**整体**相似度。拿一句 10 字的提问去比一段 800 字的
            chunk，整体相似度必然很低（分母是长文本）→ 阈值怎么调都召回不出来。
            `word_similarity(query, text)` 衡量的是"**query 有多像 text 里的某一小段**"，
            正是"短问句 vs 长文档"这个场景要的东西（pg_trgm ≥ 1.6）。

        ⚠️ 这里用**函数**而非 `%>` 算子：算子依赖会话级阈值 `pg_trgm.word_similarity_threshold`，
            而连接是池化的，会话状态容易串味。函数 + 显式阈值是确定性的、可测的。
            代价：用不上 GIN 索引（迁移里那个 `gin_trgm_ops` 索引主要服务 `ILIKE`）。
            当前知识库规模下全表算一遍完全可接受；真长大了再换算子形式。
        """
        similarity = func.word_similarity(query, Chunk.content)
        stmt = (
            self._repo.select()
            .add_columns(similarity.label("score"))
            .where(similarity >= min_similarity)
            .order_by(similarity.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                score=float(score),
            )
            for chunk, score in rows
        ]

    async def _vector(self, embedding: Sequence[float], *, limit: int) -> list[RetrievedChunk]:
        """向量路：pgvector 的余弦距离，越近越好。

        ⚠️ 一并返回**余弦相似度**（`1 - 距离`），因为拒答阈值要用它 ——
            距离越小越好、相似度越大越好，两者方向相反，混用极易写错。
        """
        distance = Chunk.embedding.cosine_distance(list(embedding))
        stmt = (
            self._repo.select()
            .add_columns(distance.label("distance"))
            .where(Chunk.embedding.is_not(None))
            .order_by(distance.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                score=1.0 - float(dist),
            )
            for chunk, dist in rows
        ]

    async def search(
        self,
        *,
        query: str,
        query_embedding: Sequence[float] | None,
        top_k: int,
        rrf_k: int,
        fulltext_min_similarity: float,
    ) -> tuple[list[RetrievedChunk], dict[uuid.UUID, float]]:
        """跑两条路并融合。

        Returns:
            `(按 RRF 排序的候选, {chunk_id: 向量余弦相似度})`。

            ⚠️ 第二个返回值**只含向量路**命中的文档，且是**余弦相似度**（越大越相关）。
                它是上层判拒答的唯一可信来源之一 ——
                全文路的 trgm 分数是另一种量纲，混进来就是瞎判。
        """
        by_id: dict[uuid.UUID, RetrievedChunk] = {}

        fulltext_hits = await self._fulltext(
            query, limit=top_k, min_similarity=fulltext_min_similarity
        )
        for hit in fulltext_hits:
            by_id[hit.chunk_id] = hit

        vector_hits: list[RetrievedChunk] = []
        if query_embedding is not None:
            vector_hits = await self._vector(query_embedding, limit=top_k)
            for hit in vector_hits:
                # 向量路的 score 是"相似度"语义，比全文路的 trgm 分数更适合对外展示，
                # 所以两路都命中时以向量那一条为准。
                by_id[hit.chunk_id] = hit

        fused = reciprocal_rank_fusion(
            [[hit.chunk_id for hit in vector_hits], [hit.chunk_id for hit in fulltext_hits]],
            k=rrf_k,
        )

        ordered = sorted(by_id.values(), key=lambda hit: fused.get(hit.chunk_id, 0.0), reverse=True)
        vector_scores = {hit.chunk_id: hit.score for hit in vector_hits}
        return ordered, vector_scores


__all__ = [
    "HybridRetriever",
    "RetrievedChunk",
    "reciprocal_rank_fusion",
]
