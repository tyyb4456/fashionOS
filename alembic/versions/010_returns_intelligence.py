"""Add reason_breakdown, evidence columns to return_insights.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-07-10 00:00:00.000001

Returns Agent rewrite: reason classification, return-rate math, severity,
fix_type, and recommended_fix are now computed deterministically in Python
(agents/returns/graph.py::compute_return_plan) via the fashion_returns
skill's keyword taxonomy. reason_breakdown and evidence were already being
generated (by the LLM, pre-rewrite) but were never persisted — dropped
between state and the DB write. This migration closes that gap.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("return_insights", sa.Column("reason_breakdown", sa.JSON(), nullable=True))
    op.add_column("return_insights", sa.Column("evidence", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("return_insights", "evidence")
    op.drop_column("return_insights", "reason_breakdown")