"""Add deterministic pricing intelligence columns to pricing_actions.

Revision ID: d9e0f1a2b3c4
Revises: b7c8d9e0f1a2
Create Date: 2026-07-09 00:00:00.000000

Pricing Agent rewrite: trending detection, markdown ladder progression,
psychological pricing, margin estimation, and discount code generation are
now computed deterministically in Python (agents/pricing/graph.py::
compute_pricing_plan) instead of by the LLM. These columns persist that
computed plan so the dashboard can show trigger context, ladder position,
margin, and discount codes without re-deriving them.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pricing_actions", sa.Column("trigger", sa.String(50), nullable=False, server_default="healthy"))
    op.add_column("pricing_actions", sa.Column("markdown_rung", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("pricing_actions", sa.Column("estimated_unit_cost_pkr", sa.Float(), nullable=True))
    op.add_column("pricing_actions", sa.Column("estimated_margin_pct", sa.Float(), nullable=True))
    op.add_column("pricing_actions", sa.Column("suggested_discount_code", sa.String(100), nullable=True))
    op.add_column("pricing_actions", sa.Column("new_compare_at_price", sa.Float(), nullable=True))
    op.create_index("ix_pricing_actions_trigger", "pricing_actions", ["trigger"])


def downgrade() -> None:
    op.drop_index("ix_pricing_actions_trigger", table_name="pricing_actions")
    op.drop_column("pricing_actions", "new_compare_at_price")
    op.drop_column("pricing_actions", "suggested_discount_code")
    op.drop_column("pricing_actions", "estimated_margin_pct")
    op.drop_column("pricing_actions", "estimated_unit_cost_pkr")
    op.drop_column("pricing_actions", "markdown_rung")
    op.drop_column("pricing_actions", "trigger")