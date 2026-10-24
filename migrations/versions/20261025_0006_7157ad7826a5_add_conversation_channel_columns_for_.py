"""add conversation channel columns for feishu bucketing

Revision ID: 7157ad7826a5
Revises: 10cd5033d34e
Create Date: 2026-10-25 00:06:45.932897+08:00

走查 P1-⑥：会话按**渠道**分开，飞书群聊还要**按人分桶**。

⚠️ 迁移脚本一旦合进主干就**不要改**（已执行过的环境不会重跑）。
    要修就新增一个迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7157ad7826a5"
down_revision: str | None = "10cd5033d34e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # `server_default='web'` 不只是给"以后的新行"用的 —— 它同时**回填了已有的行**
    # （老数据全是网页端开的会话）。没有它，`nullable=False` 在非空表上直接失败。
    op.add_column(
        "conversations",
        sa.Column(
            "channel",
            sa.Enum(
                "web",
                "feishu_p2p",
                "feishu_group",
                name="conversation_channel",
                native_enum=False,
                length=32,
            ),
            server_default="web",
            nullable=False,
        ),
    )
    op.add_column(
        "conversations", sa.Column("external_chat_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("external_user_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("external_title", sa.String(length=200), nullable=True)
    )
    # ⚠️ `timezone=True`：和 `created_at` / `updated_at` 保持同一口径（否则又会出现
    #    "有的列带时区、有的不带"，比对时间时静默错 8 小时）。
    op.add_column(
        "conversations", sa.Column("external_title_at", sa.DateTime(timezone=True), nullable=True)
    )

    # 枚举列的 CHECK（`enum_check()` 生成的那条）。autogenerate **不检测**表级 CHECK，
    # 所以必须手写 —— 漏了它，`alembic check` 看不出差别，但库里就少了这道防线。
    op.create_check_constraint(
        op.f("ck_conversations_channel"),
        "conversations",
        "channel IN ('web', 'feishu_p2p', 'feishu_group')",
    )

    # 外部会话的去重键。⚠️ `web` 那三列是 NULL，而 PG 认为 NULL 互不相等
    # ⟹ 网页端会话不受这条唯一约束影响。
    op.create_index(
        "uq_conversations_external",
        "conversations",
        ["tenant_id", "channel", "external_chat_id", "external_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_external", table_name="conversations")
    op.drop_constraint(op.f("ck_conversations_channel"), "conversations", type_="check")
    op.drop_column("conversations", "external_title_at")
    op.drop_column("conversations", "external_title")
    op.drop_column("conversations", "external_user_id")
    op.drop_column("conversations", "external_chat_id")
    op.drop_column("conversations", "channel")
