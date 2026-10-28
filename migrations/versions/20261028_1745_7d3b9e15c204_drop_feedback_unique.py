"""feedback：取消「同一人 ＋ 同一消息唯一」，改成每次上报一条新记录

Revision ID: 7d3b9e15c204
Revises: 9a1f4c7d2e80
Create Date: 2026-10-28 17:45:00

⚠️ 只删约束，**不动数据**：已有的行保持原样（它们本来就是"最后一次的说法"）。
⚠️ 删掉约束后"重复上报"靠前端挡（提交后短时禁用 ＋ 明确的「再报一次」入口），
   后端**不再替人吞证据** —— 理由见 `app/db/models/feedback.py` 的类注释。
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d3b9e15c204"
down_revision: str | None = "9a1f4c7d2e80"
branch_labels = None
depends_on = None

CONSTRAINT = "feedback_unique_per_user_message"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "answer_feedback", type_="unique")


def downgrade() -> None:
    # ⚠️ 回滚时如果已经有重复行，这个约束建不回去 —— 这正是"不再唯一"的证据，
    #    要让回滚的人先手工清理（而不是自动删掉别人上报的记录）。
    op.create_unique_constraint(CONSTRAINT, "answer_feedback", ["message_id", "user_id"])
