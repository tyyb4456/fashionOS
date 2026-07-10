"""Add deterministic restock intelligence columns to restock_recommendations.

Revision ID: b7c8d9e0f1a2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-08 00:00:00.000000

Restock Agent rewrite: quantity, supplier classification, dates, and cost
estimates are now computed deterministically in Python (agents/restock/graph.py
::compute_restock_plan) instead of by the LLM. These columns persist that
computed plan so the dashboard can show supplier type, lead time, order
deadline, overdue status, and cost estimates without re-deriving them.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("restock_recommendations", sa.Column("supplier_type", sa.String(30), nullable=False, server_default="lahore_local"))
    op.add_column("restock_recommendations", sa.Column("estimated_lead_days", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("restock_recommendations", sa.Column("expected_stockout_date", sa.Date(), nullable=True))
    op.add_column("restock_recommendations", sa.Column("order_deadline", sa.Date(), nullable=True))
    op.add_column("restock_recommendations", sa.Column("is_overdue", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("restock_recommendations", sa.Column("estimated_unit_cost_pkr", sa.Float(), nullable=True))
    op.add_column("restock_recommendations", sa.Column("estimated_total_cost_pkr", sa.Float(), nullable=True))
    op.add_column("restock_recommendations", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_restock_priority", "restock_recommendations", ["priority"])
    op.create_index("ix_restock_is_overdue", "restock_recommendations", ["is_overdue"])


def downgrade() -> None:
    op.drop_index("ix_restock_is_overdue", table_name="restock_recommendations")
    op.drop_index("ix_restock_priority", table_name="restock_recommendations")
    op.drop_column("restock_recommendations", "priority")
    op.drop_column("restock_recommendations", "estimated_total_cost_pkr")
    op.drop_column("restock_recommendations", "estimated_unit_cost_pkr")
    op.drop_column("restock_recommendations", "is_overdue")
    op.drop_column("restock_recommendations", "order_deadline")
    op.drop_column("restock_recommendations", "expected_stockout_date")
    op.drop_column("restock_recommendations", "estimated_lead_days")
    op.drop_column("restock_recommendations", "supplier_type")