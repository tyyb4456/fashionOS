"""Add chat_subagent_results table.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-28 00:00:00.000000

Persists structured subagent output per chat turn so the frontend can
render rich agent cards when loading conversation history, without
relying on client-side memory caches.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_subagent_results",
        sa.Column("id",         postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("brand_id",   sa.String(255), nullable=False),
        sa.Column("thread_id",  sa.String(255), nullable=False),
        sa.Column("turn_index", sa.Integer(),   nullable=False),
        sa.Column("agent_name", sa.String(100), nullable=False),
        sa.Column("summary",    sa.Text(),      nullable=True),
        sa.Column("data",       sa.JSON(),      nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chat_subagent_results_brand_thread",
        "chat_subagent_results",
        ["brand_id", "thread_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_subagent_results_brand_thread", table_name="chat_subagent_results")
    op.drop_table("chat_subagent_results")
