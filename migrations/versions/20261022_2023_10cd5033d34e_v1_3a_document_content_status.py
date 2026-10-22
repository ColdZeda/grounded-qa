"""v1_3a_document_content_status

Revision ID: 10cd5033d34e
Revises: e941de5ce5c8
Create Date: 2026-10-22 20:23:08.418863+08:00

v1.3 **阶段 A**：`documents` 加"内容有效性"三列 ＋ 回填。

⚠️ **本迁移是零行为变化的**：召回 SQL 一条都没改，`RETRIEVAL_SCOPE` 也还没加。
   验收判据 = 跑 `--scope all`，**逐题分数与加列前完全一致** ⟹ 证明只加了数据。
   真正的过滤在阶段 B（`architecture.md` §9.36 / `PLAN §8.6`）。

⚠️ 迁移脚本一旦合进主干就**不要改**（已执行过的环境不会重跑）。
    要修就新增一个迁移。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "10cd5033d34e"
down_revision: str | None = "e941de5ce5c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTENT_STATUSES = ("active", "superseded", "draft", "sibling_brand", "internal")


def upgrade() -> None:
    # ① 加列。`server_default='active'` 是**必须的**：存量行要和旧行为完全一致，
    #    否则 `ADD COLUMN ... NOT NULL` 在已有数据上直接失败。
    op.add_column(
        "documents",
        sa.Column(
            "content_status",
            sa.Enum(
                *_CONTENT_STATUSES, name="document_content_status", native_enum=False, length=32
            ),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column("documents", sa.Column("effective_from", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("effective_to", sa.Date(), nullable=True))

    # ② CHECK 约束 —— ⚠️ **autogenerate 不会生成它**（`AGENTS §6` 那条坑），手工补。
    #    名字必须和模型里 `enum_check(...)` 生成的一致，否则 `alembic check` 会喊不一致。
    op.create_check_constraint(
        op.f("ck_documents_document_content_status"),
        "documents",
        "content_status IN ('active', 'superseded', 'draft', 'sibling_brand', 'internal')",
    )
    op.create_index(
        op.f("ix_documents_content_status"), "documents", ["content_status"], unique=False
    )

    # ③ 回填。⚠️ 这段 SQL 和 `app/kb/ingest.py::classify_content_status` **必须一一对应**
    #    （同一份映射，两个实现 —— 那边有单测钉死，这边只有这一次机会）。
    #    目录 → 状态
    op.execute(
        "UPDATE documents SET content_status = 'superseded' "
        "WHERE filename LIKE '%/90-干扰-历史版本/%'"
    )
    op.execute(
        "UPDATE documents SET content_status = 'sibling_brand' "
        "WHERE filename LIKE '%/91-干扰-森屿Lab/%'"
    )
    op.execute(
        "UPDATE documents SET content_status = 'draft' WHERE filename LIKE '%/92-干扰-其他地区/%'"
    )
    op.execute(
        "UPDATE documents SET content_status = 'internal' WHERE filename LIKE '%/93-干扰-内部FAQ/%'"
    )
    # 例外：变更公告 / 辟谣澄清本身是"现行"的，引给用户正好
    op.execute(
        "UPDATE documents SET content_status = 'active' "
        "WHERE filename LIKE '%/90-干扰-历史版本/06-%' "
        "   OR filename LIKE '%/92-干扰-其他地区/04-%'"
    )
    # 生效区间：2023 版政策的原文横幅写着"已于 2025-12-31 废止"
    op.execute(
        "UPDATE documents "
        "SET effective_from = DATE '2023-01-01', effective_to = DATE '2025-12-31' "
        "WHERE filename LIKE '%2023版%'"
    )
    # 现行政策从 2026-01-01 起
    op.execute(
        "UPDATE documents SET effective_from = DATE '2026-01-01' "
        "WHERE effective_from IS NULL "
        "  AND (filename LIKE '%/00-%' OR filename LIKE '%/10-%' OR filename LIKE '%/20-%' "
        "       OR filename LIKE '%/30-%' OR filename LIKE '%/40-%')"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_documents_content_status"), table_name="documents")
    op.drop_constraint(op.f("ck_documents_document_content_status"), "documents", type_="check")
    op.drop_column("documents", "effective_to")
    op.drop_column("documents", "effective_from")
    op.drop_column("documents", "content_status")
