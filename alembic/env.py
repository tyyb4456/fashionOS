"""
Alembic Migration Environment
==============================
Configures Alembic to use:
  - The DATABASE_URL environment variable (same source as the app)
  - A SYNC PostgreSQL driver (psycopg2) for running migrations
    (asyncpg is async-only — Alembic's migration runner is sync)
  - Our db.models.Base metadata for --autogenerate support

The URL transformation:
  App:     postgresql+asyncpg://fashionos:pass@localhost:5432/fashionos
  Alembic: postgresql://fashionos:pass@localhost:5432/fashionos
  (just drops the +asyncpg driver qualifier)

For --autogenerate to work, this file MUST import all models before
`target_metadata` is set. The `from db.models import Base` import below
triggers SQLAlchemy to register all table definitions.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Put project root on the path (so `from db.models import Base` resolves) ──
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db.models import Base   # noqa: E402 — must be after sys.path setup

# ── Alembic Config object ─────────────────────────────────────────────────────
config = context.config

# ── Override sqlalchemy.url from environment ──────────────────────────────────
# Convert async driver to sync for Alembic's synchronous migration runner.
async_url = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
)
sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
config.set_main_option("sqlalchemy.url", sync_url)

# ── Logging setup ─────────────────────────────────────────────────────────────
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Tell Alembic which metadata to compare against for --autogenerate ─────────
target_metadata = Base.metadata


# ── Migration runners ─────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without connecting to DB.
    Useful for reviewing SQL before applying, or running in restricted envs.

    Usage:  alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,     # detect column type changes
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to DB and applies directly.
    This is the default mode used by `alembic upgrade head`.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,   # no pooling needed for one-shot migration
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()