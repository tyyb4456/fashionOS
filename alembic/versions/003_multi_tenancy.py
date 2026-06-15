"""Add brands and api_keys tables for multi-tenant SaaS.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2025-06-13 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # brands

    op.create_table(
        "brands",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("brand_id",    sa.String(100),  nullable=False),
        sa.Column("brand_name",  sa.String(255),  nullable=False),
        sa.Column("owner_email", sa.String(255),  nullable=False),
        sa.Column("plan",        sa.String(50),   nullable=False, server_default="starter"),
        sa.Column("is_active",   sa.Boolean(),    nullable=False, server_default="true"),

        sa.Column("shopify_shop_name",          sa.String(255), nullable=True),
        sa.Column("shopify_access_token_enc",   sa.Text(),      nullable=True),
        sa.Column("shopify_webhook_secret_enc", sa.Text(),      nullable=True),

        sa.Column("meta_access_token_enc", sa.Text(),        nullable=True),
        sa.Column("meta_ad_account_id",    sa.String(255),   nullable=True),

        sa.Column("instagram_access_token_enc", sa.Text(),        nullable=True),
        sa.Column("instagram_page_id",          sa.String(255),   nullable=True),

        sa.Column("brand_owner_whatsapp", sa.String(50),  nullable=True),
        sa.Column("brand_owner_email",    sa.String(255), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_brands_brand_id",    "brands", ["brand_id"],    unique=True)
    op.create_index("ix_brands_owner_email", "brands", ["owner_email"], unique=True)


    # ── api_keys ──────────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id",       postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("brand_id", sa.String(100), nullable=False),

        sa.Column("key_prefix",   sa.String(20),  nullable=False),
        sa.Column("key_hash",     sa.String(64),  nullable=False),
        sa.Column("label",        sa.String(255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active",    sa.Boolean(),   nullable=False, server_default="true"),

        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_api_keys_brand_id",  "api_keys", ["brand_id"])
    op.create_index("ix_api_keys_key_hash",  "api_keys", ["key_hash"],  unique=True)
    op.create_index("ix_api_keys_key_prefix","api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("brands")