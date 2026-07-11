"""Add dm_replies table; add dm_auto_replied, dm_flagged_open cached columns to agent_runs.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-10 00:00:00.000002

DM Agent rewrite: classification (LLM) and gating (auto_send/flag_for_human/
flag_priority — a fixed lookup off the fashion_dm skill) are now separate
graph nodes, same split applied to the Returns Agent. This also closes a gap
that predates that rewrite entirely: dm_replies were never persisted to
Postgres at all (see api/workers/tasks.py's old "not persisted to DB yet"
comment) — they only ever lived in the SSE run summary. This migration adds
the table. Spam is intentionally never persisted here — same noise-reduction
pattern as healthy SKUs in return_insights.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("dm_auto_replied", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agent_runs", sa.Column("dm_flagged_open", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "dm_replies",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),

        sa.Column("message_id",      sa.String(255), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("user_id",         sa.String(255), nullable=False),
        sa.Column("username",        sa.String(255), nullable=False, server_default=""),

        sa.Column("original_message", sa.Text(),     nullable=False, server_default=""),
        sa.Column("category",         sa.String(50), nullable=False),

        sa.Column("auto_send",      sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("flag_for_human", sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("flag_priority",  sa.String(20), nullable=True),
        sa.Column("flag_reason",    sa.Text(),     nullable=True),

        sa.Column("reply_text", sa.Text(),    nullable=True),
        sa.Column("auto_sent",  sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sent_at",    sa.DateTime(timezone=True), nullable=True),

        # "auto_sent" | "send_failed" | "flagged_open" | "flagged_resolved"
        sa.Column("status", sa.String(30), nullable=False, server_default="flagged_open"),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_dm_replies_run_id",     "dm_replies", ["run_id"])
    op.create_index("ix_dm_replies_brand_id",   "dm_replies", ["brand_id"])
    op.create_index("ix_dm_replies_message_id", "dm_replies", ["message_id"])
    op.create_index("ix_dm_replies_status",     "dm_replies", ["status"])
    op.create_index("ix_dm_replies_category",   "dm_replies", ["category"])


def downgrade() -> None:
    op.drop_index("ix_dm_replies_category",   table_name="dm_replies")
    op.drop_index("ix_dm_replies_status",     table_name="dm_replies")
    op.drop_index("ix_dm_replies_message_id", table_name="dm_replies")
    op.drop_index("ix_dm_replies_brand_id",   table_name="dm_replies")
    op.drop_index("ix_dm_replies_run_id",     table_name="dm_replies")
    op.drop_table("dm_replies")

    op.drop_column("agent_runs", "dm_flagged_open")
    op.drop_column("agent_runs", "dm_auto_replied")