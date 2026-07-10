"""Add roas_7d, spend_7d_pkr, ctr_7d columns to marketing_actions.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-07-10 00:00:00.000000

Marketing Agent rewrite: the decision framework is now computed
deterministically in Python (agents/marketing/graph.py::compute_marketing_plan)
instead of by the LLM. current_budget_pkr / new_budget_pkr / change_pct
already existed as columns but were being written incorrectly (see
db/crud.py fix in this same pass) — no migration needed for those. This
migration only adds the performance diagnostics (roas_7d, spend_7d_pkr,
ctr_7d) that weren't persisted before.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("marketing_actions", sa.Column("roas_7d", sa.Float(), nullable=True))
    op.add_column("marketing_actions", sa.Column("spend_7d_pkr", sa.Float(), nullable=False, server_default="0"))
    op.add_column("marketing_actions", sa.Column("ctr_7d", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("marketing_actions", "ctr_7d")
    op.drop_column("marketing_actions", "spend_7d_pkr")
    op.drop_column("marketing_actions", "roas_7d")