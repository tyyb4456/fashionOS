"""Add clerk_user_id to brands.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2025-06-14 00:00:00.000000
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("brands", sa.Column("clerk_user_id", sa.String(255), nullable=True))
    op.create_index("ix_brands_clerk_user_id", "brands", ["clerk_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_brands_clerk_user_id", "brands")
    op.drop_column("brands", "clerk_user_id")