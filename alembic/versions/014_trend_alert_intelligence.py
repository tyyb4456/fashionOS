"""Add score_delta column to trend_signals.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-12 00:00:00.000002

Trend Agent alert-intelligence pass: alert eligibility (critical/info
thresholds) and history-aware duplicate-alert suppression are now computed
deterministically in Python (agents/trend/graph.py::compute_trend_alerts)
instead of being self-reported by the LLM alongside the signal judgment.
score_delta -- how much a keyword's score moved since its last recorded
reading -- is a natural byproduct of that comparison and is persisted here
so both the dashboard and future runs' history lookups can use it directly
instead of recomputing it from two rows.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trend_signals", sa.Column("score_delta", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("trend_signals", "score_delta")