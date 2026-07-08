"""Rename chat_subagent_results -> chat_tool_results, agent_name -> label; add seasonal/trend-aware inventory intelligence columns.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-07 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- chat_subagent_results -> chat_tool_results rename ---
    op.rename_table("chat_subagent_results", "chat_tool_results")
    op.alter_column("chat_tool_results", "agent_name", new_column_name="label")
    op.execute(
        "ALTER INDEX ix_chat_subagent_results_brand_thread "
        "RENAME TO ix_chat_tool_results_brand_thread"
    )

    # --- inventory_snapshots: seasonal/trend-aware columns ---
    op.add_column("inventory_snapshots", sa.Column("velocity_7d",  sa.Float(), nullable=False, server_default="0"))
    op.add_column("inventory_snapshots", sa.Column("velocity_30d", sa.Float(), nullable=False, server_default="0"))
    op.add_column("inventory_snapshots", sa.Column("velocity_trend",      sa.String(20), nullable=False, server_default="stable"))
    op.add_column("inventory_snapshots", sa.Column("velocity_confidence", sa.String(10), nullable=False, server_default="low"))

    op.add_column("inventory_snapshots", sa.Column("seasonal_multiplier_applied", sa.Float(),    nullable=False, server_default="1"))
    op.add_column("inventory_snapshots", sa.Column("seasonal_context",            sa.String(50), nullable=False, server_default="off_season"))
    op.add_column("inventory_snapshots", sa.Column("days_of_stock_remaining_unadjusted", sa.Float(), nullable=False, server_default="999"))

    op.add_column("inventory_snapshots", sa.Column("reorder_point_units",  sa.Integer(), nullable=False, server_default="0"))
    op.add_column("inventory_snapshots", sa.Column("has_pending_restock",  sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inventory_snapshots", sa.Column("pending_restock_note", sa.Text(),    nullable=True))

    op.add_column("inventory_snapshots", sa.Column("size_curve_deviation", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inventory_snapshots", sa.Column("size_curve_note",      sa.Text(),    nullable=True))


def downgrade() -> None:
    # --- inventory_snapshots columns ---
    op.drop_column("inventory_snapshots", "size_curve_note")
    op.drop_column("inventory_snapshots", "size_curve_deviation")
    op.drop_column("inventory_snapshots", "pending_restock_note")
    op.drop_column("inventory_snapshots", "has_pending_restock")
    op.drop_column("inventory_snapshots", "reorder_point_units")
    op.drop_column("inventory_snapshots", "days_of_stock_remaining_unadjusted")
    op.drop_column("inventory_snapshots", "seasonal_context")
    op.drop_column("inventory_snapshots", "seasonal_multiplier_applied")
    op.drop_column("inventory_snapshots", "velocity_confidence")
    op.drop_column("inventory_snapshots", "velocity_trend")
    op.drop_column("inventory_snapshots", "velocity_30d")
    op.drop_column("inventory_snapshots", "velocity_7d")

    # --- chat_tool_results -> chat_subagent_results rename ---
    op.execute(
        "ALTER INDEX ix_chat_tool_results_brand_thread "
        "RENAME TO ix_chat_subagent_results_brand_thread"
    )
    op.alter_column("chat_tool_results", "label", new_column_name="agent_name")
    op.rename_table("chat_tool_results", "chat_subagent_results")