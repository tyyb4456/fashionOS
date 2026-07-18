"""Widen chat_tool_results.label from VARCHAR(100) to VARCHAR(255).

Revision ID: c2afd69e59a8
Revises: 014_trend_alert_intelligence
Create Date: 2026-07-19

Problem fixed: when the AI called the same tool more than once in a single
turn (e.g. get_inventory_status x 2), the second INSERT used the same label
value as the first. Depending on the DB setup this either silently overwrote
the first row or raised a constraint error -- either way the second tool-call
card disappeared after a page reload.

Fix in streaming.py appends a "#N" suffix to duplicate labels (e.g.
"get_inventory_status#2"), which can push the string past 100 chars when the
base name is already near the limit. Also widens headroom for long
comma-joined pipeline agent lists like
"inventory,trend,pricing,marketing,content,dm,returns".
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "c2afd69e59a8"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "chat_tool_results",
        "label",
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Truncate any values that are now > 100 chars before narrowing.
    op.execute(
        "UPDATE chat_tool_results SET label = LEFT(label, 100) WHERE LENGTH(label) > 100"
    )
    op.alter_column(
        "chat_tool_results",
        "label",
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=False,
    )
