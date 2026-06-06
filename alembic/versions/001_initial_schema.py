"""initial schema — agent_runs, inventory_snapshots, pricing_actions, alerts, restock_recommendations

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2025-06-05 00:00:00.000000

Tables created:
  agent_runs                ← top-level run record, one row per pipeline invocation
  inventory_snapshots       ← per-SKU snapshot per run (Inventory Agent output)
  pricing_actions           ← per-SKU pricing decision per run (Pricing Agent output)
  alerts                    ← all agent alerts merged per run
  restock_recommendations   ← pending restock orders (pre-wired for Restock Agent)

To apply:
  alembic upgrade head

To roll back:
  alembic downgrade -1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agent_runs ────────────────────────────────────────────────────────────
    op.create_table(
        "agent_runs",
        sa.Column("id",         postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",     sa.String(36),  nullable=False),
        sa.Column("brand_id",   sa.String(255), nullable=False),
        sa.Column("brand_name", sa.String(255), nullable=False),

        # Trigger context
        sa.Column("trigger",         sa.String(50),  nullable=False),
        sa.Column("trigger_payload", sa.JSON(),       nullable=True),
        sa.Column("task_id",         sa.String(255),  nullable=True),

        # Timing
        sa.Column("started_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),

        # Run metadata
        sa.Column("agents_run",           sa.JSON(), nullable=True),
        sa.Column("run_summary",          sa.Text(), nullable=True),
        sa.Column("supervisor_reasoning", sa.Text(), nullable=True),

        # Cached aggregate counts (fast list-view queries — no JOINs needed)
        sa.Column("alert_count_critical",     sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_count_warning",      sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_count_total",        sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inventory_skus_analysed",  sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_decisions_total",  sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_auto_executed",    sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_pending_approval", sa.Integer(), nullable=False, server_default="0"),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_agent_runs_run_id",   "agent_runs", ["run_id"],   unique=True)
    op.create_index("ix_agent_runs_brand_id", "agent_runs", ["brand_id"], unique=False)

    # ── inventory_snapshots ───────────────────────────────────────────────────
    op.create_table(
        "inventory_snapshots",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),
        sa.Column("sku",      sa.String(255), nullable=False),

        sa.Column("product_title", sa.String(500), nullable=False, server_default=""),
        sa.Column("variant_title", sa.String(255), nullable=False, server_default=""),

        sa.Column("current_stock",           sa.Integer(), nullable=False, server_default="0"),
        sa.Column("units_per_day",           sa.Float(),   nullable=False, server_default="0"),
        sa.Column("days_of_stock_remaining", sa.Float(),   nullable=False, server_default="999"),
        sa.Column("urgency",                 sa.String(20), nullable=False, server_default="healthy"),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_inventory_snapshots_run_id",   "inventory_snapshots", ["run_id"])
    op.create_index("ix_inventory_snapshots_brand_id", "inventory_snapshots", ["brand_id"])
    op.create_index("ix_inventory_snapshots_sku",      "inventory_snapshots", ["sku"])

    # ── pricing_actions ───────────────────────────────────────────────────────
    op.create_table(
        "pricing_actions",
        sa.Column("id",         postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",     sa.String(36),  nullable=False),
        sa.Column("brand_id",   sa.String(255), nullable=False),
        sa.Column("sku",        sa.String(255), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=True),

        sa.Column("action",            sa.String(30), nullable=False),
        sa.Column("current_price",     sa.Float(),    nullable=False, server_default="0"),
        sa.Column("recommended_price", sa.Float(),    nullable=False, server_default="0"),
        sa.Column("discount_pct",      sa.Float(),    nullable=False, server_default="0"),
        sa.Column("auto_executed",     sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("reason",            sa.Text(),     nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_pricing_actions_run_id",   "pricing_actions", ["run_id"])
    op.create_index("ix_pricing_actions_brand_id", "pricing_actions", ["brand_id"])
    op.create_index("ix_pricing_actions_sku",      "pricing_actions", ["sku"])

    # ── alerts ────────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),

        sa.Column("level",   sa.String(20),  nullable=False),   # critical|warning|info
        sa.Column("agent",   sa.String(100), nullable=False),
        sa.Column("message", sa.Text(),      nullable=False),
        sa.Column("sku",     sa.String(255), nullable=True),

        # Preserved from agent — NOT server_default
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alerts_run_id",   "alerts", ["run_id"])
    op.create_index("ix_alerts_brand_id", "alerts", ["brand_id"])
    op.create_index("ix_alerts_level",    "alerts", ["level"])

    # ── restock_recommendations ───────────────────────────────────────────────
    # Table is pre-created now; Restock Agent will populate it once built.
    op.create_table(
        "restock_recommendations",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id",   sa.String(36),  nullable=False),
        sa.Column("brand_id", sa.String(255), nullable=False),
        sa.Column("sku",      sa.String(255), nullable=False),

        sa.Column("recommended_quantity",    sa.Integer(), nullable=False, server_default="0"),
        sa.Column("urgency",                 sa.String(20), nullable=False),
        sa.Column("days_of_stock_remaining", sa.Float(),   nullable=False),
        sa.Column("units_per_day",           sa.Float(),   nullable=False),

        sa.Column("reason",           sa.Text(), nullable=False, server_default=""),
        sa.Column("supplier_message", sa.Text(), nullable=False, server_default=""),

        # pending_approval | approved | ordered | cancelled
        sa.Column("status", sa.String(50), nullable=False, server_default="pending_approval"),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_restock_run_id",   "restock_recommendations", ["run_id"])
    op.create_index("ix_restock_brand_id", "restock_recommendations", ["brand_id"])
    op.create_index("ix_restock_sku",      "restock_recommendations", ["sku"])


def downgrade() -> None:
    # Drop in reverse creation order (no FK constraints, so order doesn't matter
    # for data integrity, but keeps it clean)
    op.drop_table("restock_recommendations")
    op.drop_table("alerts")
    op.drop_table("pricing_actions")
    op.drop_table("inventory_snapshots")
    op.drop_table("agent_runs")