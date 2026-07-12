"""Add trigger, trend_score, discount_pct columns to content_posts.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-07-11 00:00:00.000000

Content Agent rewrite: posting times, sale_mention text, and hashtags are
now computed deterministically in Python (agents/content/graph.py::
compute_content_plan) via keyword rules on brand-controlled product text
plus the seasonal demand calendar (agents/seasonal.py), instead of being
asked of the LLM alongside the creative copy. trigger/trend_score/
discount_pct persist WHY each post was selected (trending vs on-sale) so
the dashboard can show it without re-deriving it from the caption text.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("content_posts", sa.Column("trigger", sa.String(30), nullable=False, server_default="on_sale"))
    op.add_column("content_posts", sa.Column("trend_score", sa.Float(), nullable=True))
    op.add_column("content_posts", sa.Column("discount_pct", sa.Float(), nullable=False, server_default="0"))
    op.create_index("ix_content_posts_trigger", "content_posts", ["trigger"])


def downgrade() -> None:
    op.drop_index("ix_content_posts_trigger", table_name="content_posts")
    op.drop_column("content_posts", "discount_pct")
    op.drop_column("content_posts", "trend_score")
    op.drop_column("content_posts", "trigger")