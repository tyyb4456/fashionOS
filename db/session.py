"""
FashionOS Database Session
===========================
Async SQLAlchemy engine + session factory.

Two consumers:
  1. Celery tasks (sync context)  — use AsyncSessionLocal directly in _run_async()
  2. FastAPI routes (async)       — use get_session() as a Depends() injection

Engine is module-level (one per process). Sessions are created per-operation
and always closed via context manager — never leaked.

Connection URL format:
  App (async):  postgresql+asyncpg://user:pass@host:5432/db
  Alembic sync: postgresql://user:pass@host:5432/db   ← set in alembic/env.py

Add ?ssl=require to DATABASE_URL for managed cloud Postgres (Supabase, Railway, etc.)
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ── Engine ─────────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,           # set to True to log all SQL in dev (noisy but useful)
    pool_pre_ping=True,   # issue a lightweight ping before using a pooled connection
    pool_size=5,          # baseline pool (plenty for 4 Celery workers)
    max_overflow=10,      # burst connections beyond pool_size
    pool_recycle=3600,    # recycle connections after 1 hour to avoid stale connections
)

# ── Session factory ────────────────────────────────────────────────────────────

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # objects remain accessible after commit without a re-fetch
    autoflush=False,         # explicit control — flush before queries, not automatically
)


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a scoped async session per HTTP request. FastAPI closes it on response.

    Usage in a router:
        from fastapi import Depends
        from sqlalchemy.ext.asyncio import AsyncSession
        from db.session import get_session

        @router.get("/runs")
        async def list_runs(
            brand_id: str,
            session: AsyncSession = Depends(get_session),
        ):
            return await crud.list_runs(session, brand_id=brand_id)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()