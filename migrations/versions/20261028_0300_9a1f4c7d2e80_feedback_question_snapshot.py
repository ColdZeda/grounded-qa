"""feedback：存下「客户问的那句」与原会话（走查-第三次 D3）

Revision ID: 9a1f4c7d2e80
Revises: 26c6d9f049b1
Create Date: 2026-10-28 03:00:00

⚠️ **只加列、不回填**：迁移之前产生的那几条 `question_snapshot` 是 NULL，
   界面上要明说「这条反馈收集时还没存提问」，而不是显示成空字符串（那看起来像"客户什么都没问"）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9a1f4c7d2e80"
down_revision: str | None = "26c6d9f049b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("answer_feedback", sa.Column("question_snapshot", sa.Text(), nullable=True))
    op.add_column(
        "answer_feedback",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "answer_feedback_conversation_id_fkey",
        "answer_feedback",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_answer_feedback_conversation_id", "answer_feedback", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_answer_feedback_conversation_id", table_name="answer_feedback")
    op.drop_constraint(
        "answer_feedback_conversation_id_fkey", "answer_feedback", type_="foreignkey"
    )
    op.drop_column("answer_feedback", "conversation_id")
    op.drop_column("answer_feedback", "question_snapshot")
