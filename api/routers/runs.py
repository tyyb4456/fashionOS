"""
FashionOS — Runs & Dashboard API Router
========================================
All read endpoints for agent run history, approval queues, and the dashboard.

Routes:
  GET /api/v1/runs                      ← run history list (sidebar)
  GET /api/v1/runs/{run_id}             ← single run detail with all child records
  GET /api/v1/runs/{run_id}/inventory   ← inventory snapshots for a specific run
  GET /api/v1/alerts/critical           ← open critical alerts (dashboard widget)
  GET /api/v1/pricing/pending           ← pricing decisions awaiting human approval
  GET /api/v1/restock/pending           ← restock orders awaiting human approval
  GET /api/v1/skus/{sku}/history        ← time-series inventory snapshots for a SKU
  GET /api/v1/dashboard                 ← aggregated home screen summary

Design decisions:
  - brand_id is a query param that falls back to BRAND_ID env var.
    This keeps single-brand usage zero-friction while supporting multi-tenant
    SaaS usage (pass brand_id explicitly per request).
  - All child records for RunDetailSchema are fetched in separate queries
    (not a JOIN) — avoids loading thousands of rows in one shot at scale.
  - DashboardSummarySchema is assembled from multiple CRUD calls in one
    route handler — no extra DB view or materialized table needed yet.
  - 404s raise HTTPException with a clear message — never return None silently.
  - All response_model= types are declared — FastAPI validates and serializes
    automatically, no manual .dict() calls needed.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db import crud
from db.schemas import (
    AlertSchema,
    DashboardSummarySchema,
    InventorySnapshotSchema,
    PricingActionSchema,
    RestockRecommendationSchema,
    RunDetailSchema,
    RunSummarySchema,
)
from db.session import get_session


router = APIRouter(prefix="/api/v1", tags=["runs"])

# Default brand (single-brand Track A mode)
BRAND_ID = os.getenv("BRAND_ID", "default-brand")


# ── Shared dependency ─────────────────────────────────────────────────────────

def _brand(brand_id: Optional[str] = Query(None, description="Brand ID. Defaults to BRAND_ID env var.")) -> str:
    """Resolves brand_id from query param or env var."""
    return brand_id or BRAND_ID


# ══════════════════════════════════════════════════════════════════════════════
# RUN HISTORY
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/runs",
    response_model=list[RunSummarySchema],
    summary="List agent run history",
    description=(
        "Returns the most recent agent pipeline runs for a brand, newest first. "
        "Each row contains cached aggregate counts (alerts, pricing decisions) — "
        "no child table JOINs. Use the run detail endpoint for full child records."
    ),
)
async def list_runs(
    brand_id: str       = Depends(_brand),
    limit:    int       = Query(50, ge=1, le=200, description="Max runs to return."),
    offset:   int       = Query(0,  ge=0,          description="Pagination offset."),
    session:  AsyncSession = Depends(get_session),
) -> list[RunSummarySchema]:
    runs = await crud.list_runs(session, brand_id=brand_id, limit=limit, offset=offset)
    return [RunSummarySchema.model_validate(r) for r in runs]


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailSchema,
    summary="Get full run detail",
    description=(
        "Returns a single run with all child records: inventory snapshots, "
        "pricing decisions, and alerts. Child records are fetched in separate "
        "queries (not a JOIN) for performance at scale."
    ),
)
async def get_run(
    run_id:  str,
    session: AsyncSession = Depends(get_session),
) -> RunDetailSchema:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    # Fetch child records in parallel queries (separate, not JOIN)
    snapshots = await crud.get_run_snapshots(session, run_id=run_id)
    pricing   = await crud.get_run_pricing(session, run_id=run_id)
    alerts    = await crud.get_run_alerts(session, run_id=run_id)

    # Build detail schema — model_validate reads all scalar fields from ORM object
    detail = RunDetailSchema.model_validate(run)
    detail.inventory_snapshots = [InventorySnapshotSchema.model_validate(s) for s in snapshots]
    detail.pricing_actions     = [PricingActionSchema.model_validate(p) for p in pricing]
    detail.alerts              = [AlertSchema.model_validate(a) for a in alerts]

    return detail


@router.get(
    "/runs/{run_id}/inventory",
    response_model=list[InventorySnapshotSchema],
    summary="Get inventory snapshots for a run",
    description="Returns all per-SKU inventory snapshots for a run, sorted by urgency (most critical first).",
)
async def get_run_inventory(
    run_id:  str,
    session: AsyncSession = Depends(get_session),
) -> list[InventorySnapshotSchema]:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")

    snapshots = await crud.get_run_snapshots(session, run_id=run_id)
    return [InventorySnapshotSchema.model_validate(s) for s in snapshots]


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/alerts/critical",
    response_model=list[AlertSchema],
    summary="Get recent critical alerts",
    description=(
        "Returns the most recent critical-level alerts across all runs for a brand. "
        "Used by the dashboard header/notification widget. "
        "Includes alerts from all agents (inventory, pricing, restock, etc.)."
    ),
)
async def get_critical_alerts(
    brand_id: str       = Depends(_brand),
    limit:    int       = Query(20, ge=1, le=100),
    session:  AsyncSession = Depends(get_session),
) -> list[AlertSchema]:
    alerts = await crud.get_critical_alerts(session, brand_id=brand_id, limit=limit)
    return [AlertSchema.model_validate(a) for a in alerts]


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL QUEUES
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/pricing/pending",
    response_model=list[PricingActionSchema],
    summary="Get pending pricing decisions",
    description=(
        "Returns all pricing decisions that were NOT auto-executed and require "
        "human approval. Sorted by most recent run first. "
        "Excludes 'hold' decisions (already resolved). "
        "These are markdowns >15%, price increases, clearance codes, and bundles."
    ),
)
async def get_pending_pricing(
    brand_id: str       = Depends(_brand),
    limit:    int       = Query(100, ge=1, le=500),
    session:  AsyncSession = Depends(get_session),
) -> list[PricingActionSchema]:
    records = await crud.get_pending_pricing(session, brand_id=brand_id, limit=limit)
    return [PricingActionSchema.model_validate(r) for r in records]


@router.get(
    "/restock/pending",
    response_model=list[RestockRecommendationSchema],
    summary="Get pending restock orders",
    description=(
        "Returns all restock recommendations awaiting human approval, "
        "sorted by most urgent (lowest days of stock remaining first). "
        "All restock orders are always pending — none are auto-approved."
    ),
)
async def get_pending_restocks(
    brand_id: str       = Depends(_brand),
    session:  AsyncSession = Depends(get_session),
) -> list[RestockRecommendationSchema]:
    records = await crud.get_pending_restocks(session, brand_id=brand_id)
    return [RestockRecommendationSchema.model_validate(r) for r in records]


# ══════════════════════════════════════════════════════════════════════════════
# SKU TIME-SERIES
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/skus/{sku}/history",
    response_model=list[InventorySnapshotSchema],
    summary="Get inventory history for a SKU",
    description=(
        "Returns time-series inventory snapshots for a single SKU across runs. "
        "Useful for trend charts on the product detail page — "
        "shows how days_of_stock_remaining and urgency have changed over time."
    ),
)
async def get_sku_history(
    sku:      str,
    brand_id: str       = Depends(_brand),
    limit:    int       = Query(30, ge=1, le=100, description="Max historical snapshots to return."),
    session:  AsyncSession = Depends(get_session),
) -> list[InventorySnapshotSchema]:
    records = await crud.get_sku_history(session, brand_id=brand_id, sku=sku, limit=limit)
    if not records:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No inventory history found for SKU '{sku}' in brand '{brand_id}'.",
        )
    return [InventorySnapshotSchema.model_validate(r) for r in records]


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/dashboard",
    response_model=DashboardSummarySchema,
    summary="Get dashboard home screen summary",
    description=(
        "Single-call aggregated payload for the dashboard home screen. "
        "Combines: recent runs, critical alerts, pending queue counts, and last run summary. "
        "Designed so the frontend needs exactly one API call to render the home view."
    ),
)
async def get_dashboard(
    brand_id:     str       = Depends(_brand),
    recent_limit: int       = Query(10, ge=1, le=50, description="Number of recent runs to include."),
    session:      AsyncSession = Depends(get_session),
) -> DashboardSummarySchema:
    # All queries run sequentially — small enough that parallelism isn't needed yet
    recent_runs     = await crud.list_runs(session, brand_id=brand_id, limit=recent_limit)
    critical_alerts = await crud.get_critical_alerts(session, brand_id=brand_id, limit=10)
    pending_pricing = await crud.get_pending_pricing(session, brand_id=brand_id, limit=500)
    pending_restock = await crud.get_pending_restocks(session, brand_id=brand_id)

    # Derive last run info from the most recent record
    last_run = recent_runs[0] if recent_runs else None

    # Count runs that started today (UTC)
    today = datetime.now(timezone.utc).date()
    total_runs_today = sum(
        1 for r in recent_runs
        if r.created_at.date() == today
    )

    return DashboardSummarySchema(
        brand_id                  = brand_id,
        last_run_at               = last_run.completed_at if last_run else None,
        last_run_summary          = last_run.run_summary if last_run else None,
        total_runs_today          = total_runs_today,
        critical_alerts_open      = len(critical_alerts),
        pending_pricing_decisions = len(pending_pricing),
        pending_restock_orders    = len(pending_restock),
        recent_runs               = [RunSummarySchema.model_validate(r) for r in recent_runs],
        critical_alerts           = [AlertSchema.model_validate(a) for a in critical_alerts],
    )