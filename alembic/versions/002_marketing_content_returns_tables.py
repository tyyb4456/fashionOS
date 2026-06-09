"""Add marketing_actions, content_posts, return_insights tables;
add marketing cached columns to agent_runs.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2025-06-09 00:00:00.000000

Tables created:
  marketing_actions   ← per-campaign budget decisions (Marketing Agent)
  content_posts       ← generated Instagram + TikTok content (Content Agent)
  return_insights     ← structured return patterns (Returns Agent)

Columns added to agent_runs:
  marketing_decisions_total
  marketing_auto_executed
  marketing_pending_approval

To apply:
  alembic upgrade head

To roll back:
  alembic downgrade -1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── Add marketing columns to agent_runs ───────────────────────────────────
    op.add_column("agent_runs", sa.Column(
        "marketing_decisions_total", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("agent_runs", sa.Column(
        "marketing_auto_executed", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("agent_runs", sa.Column(
        "marketing_pending_approval", sa.Integer(), nullable=False, server_default="0"
    ))

    # ── marketing_actions ─────────────────────────────────────────────────────
    op.create_table(
        "marketing_actions",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),

        sa.Column("sku",           sa.String(255), nullable=True),
        sa.Column("campaign_id",   sa.String(255), nullable=False),
        sa.Column("campaign_name", sa.String(500), nullable=False),

        sa.Column("action",              sa.String(50),  nullable=False),
        sa.Column("current_budget_pkr",  sa.Float(),     nullable=False, server_default="0"),
        sa.Column("new_budget_pkr",      sa.Float(),     nullable=True),
        sa.Column("change_pct",          sa.Float(),     nullable=False, server_default="0"),
        sa.Column("auto_executed",       sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("reason",              sa.Text(),      nullable=True),
        sa.Column("trigger",             sa.String(50),  nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_marketing_actions_run_id",    "marketing_actions", ["run_id"])
    op.create_index("ix_marketing_actions_brand_id",  "marketing_actions", ["brand_id"])
    op.create_index("ix_marketing_actions_sku",       "marketing_actions", ["sku"])
    op.create_index("ix_marketing_actions_campaign",  "marketing_actions", ["campaign_id"])

    # ── content_posts ─────────────────────────────────────────────────────────
    op.create_table(
        "content_posts",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),

        sa.Column("sku",           sa.String(255), nullable=False),
        sa.Column("product_title", sa.String(500), nullable=False, server_default=""),
        sa.Column("variant_title", sa.String(255), nullable=False, server_default=""),

        sa.Column("is_urgent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status",    sa.String(50), nullable=False, server_default="pending"),

        sa.Column("instagram_caption",   sa.Text(),     nullable=True),
        sa.Column("instagram_hashtags",  sa.JSON(),     nullable=True),
        sa.Column("instagram_post_time", sa.String(50), nullable=True),

        sa.Column("tiktok_script",    sa.JSON(),     nullable=True),
        sa.Column("tiktok_post_time", sa.String(50), nullable=True),

        sa.Column("creator_notes", sa.Text(),        nullable=True),
        sa.Column("sale_mention",  sa.String(500),   nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_content_posts_run_id",   "content_posts", ["run_id"])
    op.create_index("ix_content_posts_brand_id", "content_posts", ["brand_id"])
    op.create_index("ix_content_posts_sku",      "content_posts", ["sku"])
    op.create_index("ix_content_posts_urgent",   "content_posts", ["is_urgent"])

    # ── return_insights ───────────────────────────────────────────────────────
    op.create_table(
        "return_insights",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),

        sa.Column("sku",           sa.String(255), nullable=False),
        sa.Column("product_title", sa.String(500), nullable=False, server_default=""),

        sa.Column("total_returns",        sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_units_returned", sa.Integer(), nullable=False, server_default="0"),

        sa.Column("primary_reason",      sa.String(100), nullable=False),
        sa.Column("return_rate_pct",     sa.Float(),     nullable=True),
        sa.Column("estimated_30d_sales", sa.Integer(),   nullable=True),

        sa.Column("severity",        sa.String(20),  nullable=False),
        sa.Column("recommended_fix", sa.Text(),      nullable=False, server_default=""),
        sa.Column("fix_type",        sa.String(100), nullable=False, server_default="monitor"),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_return_insights_run_id",   "return_insights", ["run_id"])
    op.create_index("ix_return_insights_brand_id", "return_insights", ["brand_id"])
    op.create_index("ix_return_insights_sku",      "return_insights", ["sku"])
    op.create_index("ix_return_insights_severity", "return_insights", ["severity"])


def downgrade() -> None:
    op.drop_table("return_insights")
    op.drop_table("content_posts")
    op.drop_table("marketing_actions")

    op.drop_column("agent_runs", "marketing_pending_approval")
    op.drop_column("agent_runs", "marketing_auto_executed")
    op.drop_column("agent_runs", "marketing_decisions_total")