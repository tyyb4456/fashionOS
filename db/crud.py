"""
FashionOS CRUD Operations
==========================
All database reads and writes. No business logic — pure I/O.

Write path (called from Celery task):
  save_run(session, summary, state)
    └─ creates AgentRun + child records in one transaction

Read path (called from FastAPI routes — built later):
  list_runs()            → run history sidebar
  get_run()              → run detail page
  get_run_snapshots()    → inventory table on detail page
  get_run_pricing()      → pricing decisions table
  get_run_alerts()       → alert feed
  get_pending_restocks() → restock approval queue
  get_pending_pricing()  → pricing approval queue
  get_critical_alerts()  → dashboard alert widget

All functions take an AsyncSession — the caller owns the transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AgentRun,
    AlertRecord,
    InventorySnapshotRecord,
    PricingActionRecord,
    RestockRecommendationRecord,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — save_run
# ══════════════════════════════════════════════════════════════════════════════

async def save_run(
    session: AsyncSession,
    summary: dict,
    state: dict,
) -> Optional[AgentRun]:
    """
    Persists a completed agent pipeline run and all child records.

    Called once per Celery task completion in api/workers/tasks.py.
    The function is idempotent — if run_id already exists (Celery retry),
    it logs a warning and returns None without duplicating rows.

    Args:
        session: Async SQLAlchemy session. Caller manages the transaction.
        summary: The summary dict built in run_agent_pipeline() — contains
                 cached aggregate counts, brand info, and timing.
        state:   The full FashionOSState dict returned by supervisor_graph.ainvoke()
                 — contains inventory_snapshot, pricing_recommendations, alerts, etc.

    Returns:
        Created AgentRun record, or None if run_id already persisted.
    """
    run_id = summary["run_id"]

    # ── Idempotency guard — safe on Celery retries ────────────────────────────
    existing = await session.execute(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    if existing.scalar_one_or_none():
        logger.warning(f"[DB] run_id={run_id} already in DB — skipping duplicate save.")
        return None

    # ── 1. AgentRun (parent row) ──────────────────────────────────────────────
    alert_counts  = summary.get("alert_counts", {})
    pricing_stats = summary.get("pricing", {})

    run = AgentRun(
        run_id               = run_id,
        brand_id             = summary["brand_id"],
        brand_name           = summary["brand_name"],
        trigger              = summary["trigger"],
        trigger_payload      = state.get("trigger_payload"),
        task_id              = summary.get("task_id"),
        started_at           = _parse_dt(summary.get("started_at")) or datetime.now(timezone.utc),
        completed_at         = _parse_dt(summary.get("completed_at") or state.get("completed_at")),
        agents_run           = summary.get("completed_agents", []),
        run_summary          = summary.get("run_summary"),
        supervisor_reasoning = state.get("supervisor_reasoning"),

        # Cached counts — avoids JOIN on list view queries
        alert_count_critical     = alert_counts.get("critical", 0),
        alert_count_warning      = alert_counts.get("warning", 0),
        alert_count_total        = alert_counts.get("total", 0),
        inventory_skus_analysed  = summary.get("inventory_skus_analysed", 0),
        pricing_decisions_total  = pricing_stats.get("total_decisions", 0),
        pricing_auto_executed    = pricing_stats.get("auto_executed", 0),
        pricing_pending_approval = pricing_stats.get("pending_approval", 0),
    )
    session.add(run)

    # ── 2. Inventory snapshots ────────────────────────────────────────────────
    for snap in state.get("inventory_snapshot", []):
        session.add(InventorySnapshotRecord(
            run_id                  = run_id,
            brand_id                = summary["brand_id"],
            sku                     = snap.get("sku", ""),
            product_title           = snap.get("product_title", ""),
            variant_title           = snap.get("variant_title", ""),
            current_stock           = snap.get("current_stock", 0),
            units_per_day           = snap.get("units_per_day", 0.0),
            days_of_stock_remaining = snap.get("days_of_stock_remaining", 999.0),
            urgency                 = snap.get("urgency", "healthy"),
        ))

    # ── 3. Pricing actions ────────────────────────────────────────────────────
    for rec in state.get("pricing_recommendations", []):
        action = rec.get("action", "hold")
        # Mirrors the auto_execute logic in the Pricing Agent
        is_auto = (action == "hold") or (
            action == "markdown" and rec.get("discount_pct", 0) <= 15
        )
        session.add(PricingActionRecord(
            run_id            = run_id,
            brand_id          = summary["brand_id"],
            sku               = rec.get("sku", ""),
            variant_id        = rec.get("variant_id"),
            action            = action,
            current_price     = rec.get("current_price", 0.0),
            recommended_price = rec.get("recommended_price", 0.0),
            discount_pct      = rec.get("discount_pct", 0.0),
            auto_executed     = is_auto,
            reason            = rec.get("reason"),
        ))

    # ── 4. Alerts ─────────────────────────────────────────────────────────────
    for alert in state.get("alerts", []):
        # Preserve the agent-set timestamp; fall back to now if missing/malformed
        created_at = _parse_dt(alert.get("created_at")) or datetime.now(timezone.utc)
        session.add(AlertRecord(
            run_id     = run_id,
            brand_id   = summary["brand_id"],
            level      = alert.get("level", "info"),
            agent      = alert.get("agent", "unknown"),
            message    = alert.get("message", ""),
            sku        = alert.get("sku"),
            created_at = created_at,
        ))

    # ── 5. Restock recommendations (empty now, populated when Restock Agent lands) ──
    for rec in state.get("restock_recommendations", []):
        session.add(RestockRecommendationRecord(
            run_id                  = run_id,
            brand_id                = summary["brand_id"],
            sku                     = rec.get("sku", ""),
            recommended_quantity    = rec.get("recommended_quantity", 0),
            urgency                 = rec.get("urgency", "normal"),
            days_of_stock_remaining = rec.get("days_of_stock_remaining", 0.0),
            units_per_day           = rec.get("units_per_day", 0.0),
            reason                  = rec.get("reason", ""),
            supplier_message        = rec.get("supplier_message", ""),
            status                  = rec.get("status", "pending_approval"),
        ))

    logger.info(
        f"[DB] Queued run={run_id} | "
        f"snapshots={len(state.get('inventory_snapshot', []))} | "
        f"pricing={len(state.get('pricing_recommendations', []))} | "
        f"alerts={len(state.get('alerts', []))}"
    )
    return run


# ══════════════════════════════════════════════════════════════════════════════
# READS — Dashboard API (used when api/routers/runs.py is built)
# ══════════════════════════════════════════════════════════════════════════════

async def list_runs(
    session: AsyncSession,
    brand_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRun]:
    """Run history for the dashboard sidebar — newest first."""
    result = await session.execute(
        select(AgentRun)
        .where(AgentRun.brand_id == brand_id)
        .order_by(desc(AgentRun.created_at))
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_run(session: AsyncSession, run_id: str) -> Optional[AgentRun]:
    """Single run by its run_id (not the UUID pk)."""
    result = await session.execute(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    return result.scalar_one_or_none()


async def get_run_snapshots(
    session: AsyncSession, run_id: str
) -> list[InventorySnapshotRecord]:
    """Inventory snapshots for a run — most urgent first."""
    result = await session.execute(
        select(InventorySnapshotRecord)
        .where(InventorySnapshotRecord.run_id == run_id)
        .order_by(InventorySnapshotRecord.days_of_stock_remaining)
    )
    return list(result.scalars().all())


async def get_run_pricing(
    session: AsyncSession, run_id: str
) -> list[PricingActionRecord]:
    """All pricing decisions for a run — highest discount first."""
    result = await session.execute(
        select(PricingActionRecord)
        .where(PricingActionRecord.run_id == run_id)
        .order_by(desc(PricingActionRecord.discount_pct))
    )
    return list(result.scalars().all())


async def get_run_alerts(
    session: AsyncSession, run_id: str
) -> list[AlertRecord]:
    """All alerts for a run."""
    result = await session.execute(
        select(AlertRecord).where(AlertRecord.run_id == run_id)
    )
    return list(result.scalars().all())


async def get_pending_pricing(
    session: AsyncSession,
    brand_id: str,
    limit: int = 100,
) -> list[PricingActionRecord]:
    """
    All pricing actions NOT yet auto-executed that need human approval.
    Excludes 'hold' — those are already resolved.
    Sorted by most recent run first.
    """
    result = await session.execute(
        select(PricingActionRecord)
        .where(
            PricingActionRecord.brand_id    == brand_id,
            PricingActionRecord.auto_executed == False,   # noqa: E712
            PricingActionRecord.action       != "hold",
        )
        .order_by(desc(PricingActionRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_pending_restocks(
    session: AsyncSession,
    brand_id: str,
) -> list[RestockRecommendationRecord]:
    """
    Pending restock recommendations — sorted by most urgent (lowest days remaining).
    These are the items showing up in the dashboard's restock approval queue.
    """
    result = await session.execute(
        select(RestockRecommendationRecord)
        .where(
            RestockRecommendationRecord.brand_id == brand_id,
            RestockRecommendationRecord.status   == "pending_approval",
        )
        .order_by(RestockRecommendationRecord.days_of_stock_remaining)
    )
    return list(result.scalars().all())


async def get_critical_alerts(
    session: AsyncSession,
    brand_id: str,
    limit: int = 20,
) -> list[AlertRecord]:
    """Recent critical alerts — for the dashboard header/notification widget."""
    result = await session.execute(
        select(AlertRecord)
        .where(
            AlertRecord.brand_id == brand_id,
            AlertRecord.level    == "critical",
        )
        .order_by(desc(AlertRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_sku_history(
    session: AsyncSession,
    brand_id: str,
    sku: str,
    limit: int = 30,
) -> list[InventorySnapshotRecord]:
    """
    Time-series of inventory snapshots for a single SKU.
    Useful for trend charts on the product detail page.
    """
    result = await session.execute(
        select(InventorySnapshotRecord)
        .where(
            InventorySnapshotRecord.brand_id == brand_id,
            InventorySnapshotRecord.sku      == sku,
        )
        .order_by(desc(InventorySnapshotRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Helper ─────────────────────────────────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO datetime string into a tz-aware datetime. Returns None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None