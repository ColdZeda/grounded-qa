"""enable pg_trgm for full-text retrieval

Revision ID: 5bf353f50e41
Revises: b2372c1b37a8
Create Date: 2026-10-14 20:59:29.207135+08:00

⚠️ 迁移脚本一旦合进主干就**不要改**（已执行过的环境不会重跑）。
    要修就新增一个迁移。

⚠️ 本迁移**只装扩展，不建索引** —— 这是有意的，理由写在下面，
    不要"顺手"把索引补上，除非先确认查询真的用得上它。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5bf353f50e41"
down_revision: str | None = "b2372c1b37a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """装 `pg_trgm`。

    混合检索的**全文那一路**靠它：`pg_trgm` 提供 `word_similarity(query, text)`，
    衡量"这句短提问有多像长文本里的某一小段" —— 正是"短问句 vs 长 chunk"
    这个场景要的东西（PostgreSQL 原生 FTS 不切中文，整句话会被当成一个"词"）。

    ⚠️ **为什么这里不建 GIN 索引**（`gin_trgm_ops`）：
        GIN 的 trgm 索引只加速**算子形式**（`%`、`%>`、`ILIKE '%x%'`），
        而我们的查询用的是 `word_similarity() >= 阈值` 的**函数形式** ——
        **吃不到索引**。索引建了也用不上，白白增加每次写入 chunk 的开销。
        当前知识库规模下全表算一遍完全可接受（2C2G 上几千条 chunk 是毫秒级）。

    📌 **什么时候该把它加上**（两件事一起做，缺一不可）：

        1. 迁移里加索引
           op.create_index(
               "ix_chunks_content_trgm", "chunks", ["content"],
               postgresql_using="gin",
               postgresql_ops={"content": "gin_trgm_ops"},
           )
        2. **同时**把 `app/rag/retrieval.py` 的查询从函数形式改成算子形式
           （`content %> :q`），并用 `set_config('pg_trgm.word_similarity_threshold',
           :t, true)` 在**同一个事务里**设阈值（`is_local=true` 保证连接归还时自动复位）。

        只做 1 不做 2 = 多了一个永远不会被用到的索引。
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    """**刻意不删扩展。**

    ⚠️ 降级一个"只装扩展"的迁移，正确做法是**什么都不做**：
        `DROP EXTENSION pg_trgm` 会连带删掉所有依赖它的索引与函数调用，
        而别的迁移 / 手工建的索引可能正用着它。扩展本身不占什么资源，
        留着没有任何副作用 —— "回滚"在这里的收益远小于风险。
    """
