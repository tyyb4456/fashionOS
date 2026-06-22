"""
FashionOS — Runs & Dashboard API Router
========================================
Session 7: Added 6 new endpoints for marketing/content/returns tables.
Updated get_run() to include all child records.
Updated get_dashboard() to include all pending queue counts.

Routes:
  GET /api/v1/runs                          ← run history list
  GET /api/v1/runs/{run_id}                 ← full run detail (now incl. marketing/content/returns)
  GET /api/v1/runs/{run_id}/inventory       ← inventory snapshots
  GET /api/v1/runs/{run_id}/marketing       ← marketing decisions (NEW session 7)
  GET /api/v1/runs/{run_id}/content         ← content posts (NEW session 7)
  GET /api/v1/runs/{run_id}/returns         ← return insights (NEW session 7)
  GET /api/v1/alerts/critical               ← open critical alerts
  GET /api/v1/pricing/pending               ← pricing decisions pending approval
  GET /api/v1/restock/pending               ← restock orders pending approval
  GET /api/v1/marketing/pending             ← marketing budget changes pending (NEW session 7)
  GET /api/v1/content/queue                 ← content posts pending posting (NEW session 7)
  GET /api/v1/returns/insights              ← return fix queue (NEW session 7)
  GET /api/v1/skus/{sku}/history            ← SKU inventory time-series
  GET /api/v1/dashboard                     ← dashboard home summary (updated session 7)
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_brand
from db.models import Brand

from db import crud
from db.schemas import (
    AlertSchema,
    ContentPostSchema,
    DashboardSummarySchema,
    InventorySnapshotSchema,
    MarketingActionSchema,
    PricingActionSchema,
    RestockRecommendationSchema,
    ReturnInsightSchema,
    RunDetailSchema,
    RunSummarySchema,
)
from db.session import get_session


router = APIRouter(prefix="/api/v1", tags=["runs"])


# ══════════════════════════════════════════════════════════════════════════════
# RUN HISTORY
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/runs",
    response_model=list[RunSummarySchema],
    summary="List agent run history",
)
async def list_runs(
    brand:   Brand = Depends(get_current_brand),
    limit:   int          = Query(50, ge=1, le=200),
    offset:  int          = Query(0,  ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[RunSummarySchema]:
    runs = await crud.list_runs(session, brand_id=brand.brand_id, limit=limit, offset=offset)
    return [RunSummarySchema.model_validate(r) for r in runs]


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailSchema,
    summary="Get full run detail",
    description="Returns all child records: inventory, pricing, alerts, marketing, content, return insights.",
)
async def get_run(
    run_id:  str,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> RunDetailSchema:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")
    if run.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this run.")

    # Fetch all child records
    snapshots       = await crud.get_run_snapshots(session, run_id=run_id)
    pricing         = await crud.get_run_pricing(session, run_id=run_id)
    alerts          = await crud.get_run_alerts(session, run_id=run_id)
    marketing       = await crud.get_run_marketing(session, run_id=run_id)
    content         = await crud.get_run_content(session, run_id=run_id)
    return_insights = await crud.get_run_return_insights(session, run_id=run_id)

    detail = RunDetailSchema.model_validate(run)
    detail.inventory_snapshots = [InventorySnapshotSchema.model_validate(s) for s in snapshots]
    detail.pricing_actions     = [PricingActionSchema.model_validate(p) for p in pricing]
    detail.alerts              = [AlertSchema.model_validate(a) for a in alerts]
    detail.marketing_actions   = [MarketingActionSchema.model_validate(m) for m in marketing]
    detail.content_posts       = [ContentPostSchema.model_validate(c) for c in content]
    detail.return_insights     = [ReturnInsightSchema.model_validate(r) for r in return_insights]

    return detail


@router.get(
    "/runs/{run_id}/inventory",
    response_model=list[InventorySnapshotSchema],
    summary="Get inventory snapshots for a run",
)
async def get_run_inventory(
    run_id:  str,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> list[InventorySnapshotSchema]:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")
    if run.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this run.")
    snapshots = await crud.get_run_snapshots(session, run_id=run_id)
    return [InventorySnapshotSchema.model_validate(s) for s in snapshots]


@router.get(
    "/runs/{run_id}/marketing",
    response_model=list[MarketingActionSchema],
    summary="Get marketing decisions for a run",
    description="All Meta campaign budget/status decisions made in a specific run.",
)
async def get_run_marketing_decisions(
    run_id:  str,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> list[MarketingActionSchema]:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")
    if run.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this run.")
    records = await crud.get_run_marketing(session, run_id=run_id)
    return [MarketingActionSchema.model_validate(r) for r in records]


@router.get(
    "/runs/{run_id}/content",
    response_model=list[ContentPostSchema],
    summary="Get content posts generated in a run",
    description="Instagram captions + TikTok scripts generated by the Content Agent. Urgent posts first.",
)
async def get_run_content_posts(
    run_id:  str,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> list[ContentPostSchema]:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")
    if run.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this run.")
    records = await crud.get_run_content(session, run_id=run_id)
    return [ContentPostSchema.model_validate(r) for r in records]


@router.get(
    "/runs/{run_id}/returns",
    response_model=list[ReturnInsightSchema],
    summary="Get return insights for a run",
    description="Structured return patterns found by the Returns Agent. Critical first.",
)
async def get_run_return_insights(
    run_id:  str,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> list[ReturnInsightSchema]:
    run = await crud.get_run(session, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found.")
    if run.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this run.")
    records = await crud.get_run_return_insights(session, run_id=run_id)
    return [ReturnInsightSchema.model_validate(r) for r in records]


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/alerts/critical",
    response_model=list[AlertSchema],
    summary="Get recent critical alerts",
)
async def get_critical_alerts(
    brand:   Brand = Depends(get_current_brand),
    limit:   int          = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[AlertSchema]:
    alerts = await crud.get_critical_alerts(session, brand_id=brand.brand_id, limit=limit)
    return [AlertSchema.model_validate(a) for a in alerts]


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL QUEUES
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/pricing/pending",
    response_model=list[PricingActionSchema],
    summary="Get pending pricing decisions",
    description="Markdowns >15%, price increases, clearance codes — all awaiting human approval.",
)
async def get_pending_pricing(
    brand:   Brand = Depends(get_current_brand),
    limit:   int          = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[PricingActionSchema]:
    records = await crud.get_pending_pricing(session, brand_id=brand.brand_id, limit=limit)
    return [PricingActionSchema.model_validate(r) for r in records]


@router.get(
    "/restock/pending",
    response_model=list[RestockRecommendationSchema],
    summary="Get pending restock orders",
    description="Restock recommendations sorted by urgency (lowest days of stock first).",
)
async def get_pending_restocks(
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
) -> list[RestockRecommendationSchema]:
    records = await crud.get_pending_restocks(session, brand_id=brand.brand_id)
    return [RestockRecommendationSchema.model_validate(r) for r in records]


@router.get(
    "/marketing/pending",
    response_model=list[MarketingActionSchema],
    summary="Get pending marketing decisions",
    description=(
        "Meta campaign budget increases and activations awaiting human approval. "
        "Auto-executed actions (pause, decrease) are NOT in this queue — they're already done."
    ),
)
async def get_pending_marketing_decisions(
    brand:   Brand = Depends(get_current_brand),
    limit:   int          = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[MarketingActionSchema]:
    records = await crud.get_pending_marketing(session, brand_id=brand.brand_id, limit=limit)
    return [MarketingActionSchema.model_validate(r) for r in records]


@router.get(
    "/content/queue",
    response_model=list[ContentPostSchema],
    summary="Get content queue",
    description=(
        "Content posts pending publishing. Urgent posts (trending products) first. "
        "Use ?status=posted or ?status=skipped to view history."
    ),
)
async def get_content_queue(
    brand:   Brand = Depends(get_current_brand),
    status:  str          = Query("pending", description="pending | posted | skipped"),
    limit:   int          = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[ContentPostSchema]:
    records = await crud.get_content_queue(session, brand_id=brand.brand_id, status=status, limit=limit)
    return [ContentPostSchema.model_validate(r) for r in records]


@router.get(
    "/returns/insights",
    response_model=list[ReturnInsightSchema],
    summary="Get return insights fix queue",
    description=(
        "Structured return patterns sorted by severity. "
        "Filter by severity with ?severity=critical|warning|info. "
        "Each insight has a fix_type that maps to a specific dashboard action."
    ),
)
async def get_return_insights(
    brand:    Brand = Depends(get_current_brand),
    severity: Optional[str]      = Query(None, description="Filter: critical | warning | info"),
    limit:    int                 = Query(50, ge=1, le=200),
    session:  AsyncSession        = Depends(get_session),
) -> list[ReturnInsightSchema]:
    records = await crud.get_return_insights(session, brand_id=brand.brand_id, severity=severity, limit=limit)
    return [ReturnInsightSchema.model_validate(r) for r in records]


# ══════════════════════════════════════════════════════════════════════════════
# SKU TIME-SERIES
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/skus/{sku}/history",
    response_model=list[InventorySnapshotSchema],
    summary="Get inventory history for a SKU",
)
async def get_sku_history(
    sku:     str,
    brand:   Brand = Depends(get_current_brand),
    limit:   int          = Query(30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[InventorySnapshotSchema]:
    records = await crud.get_sku_history(session, brand_id=brand.brand_id, sku=sku, limit=limit)
    if not records:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No inventory history for SKU '{sku}' in brand '{brand.brand_id}'.",
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
        "Includes all pending queue counts, critical alerts, and recent run history. "
        "Updated in session 7 to include marketing/content/returns counts."
    ),
)
async def get_dashboard(
    brand:        Brand = Depends(get_current_brand),
    recent_limit: int          = Query(10, ge=1, le=50),
    session:      AsyncSession = Depends(get_session),
) -> DashboardSummarySchema:
    # Fetch all needed data
    recent_runs          = await crud.list_runs(session, brand_id=brand.brand_id, limit=recent_limit)
    critical_alerts      = await crud.get_critical_alerts(session, brand_id=brand.brand_id, limit=10)
    pending_pricing      = await crud.get_pending_pricing(session, brand_id=brand.brand_id, limit=500)
    pending_restock      = await crud.get_pending_restocks(session, brand_id=brand.brand_id)
    pending_marketing    = await crud.get_pending_marketing(session, brand_id=brand.brand_id)
    pending_content      = await crud.get_content_queue(session, brand_id=brand.brand_id, status="pending")
    open_return_insights = await crud.get_return_insights(session, brand_id=brand.brand_id)

    last_run = recent_runs[0] if recent_runs else None

    today = datetime.now(timezone.utc).date()
    total_runs_today = sum(
        1 for r in recent_runs
        if r.created_at.date() == today
    )

    return DashboardSummarySchema(
        brand_id                  = brand.brand_id,
        last_run_at               = last_run.completed_at if last_run else None,
        last_run_summary          = last_run.run_summary if last_run else None,
        total_runs_today          = total_runs_today,
        critical_alerts_open      = len(critical_alerts),
        pending_pricing_decisions = len(pending_pricing),
        pending_restock_orders    = len(pending_restock),
        pending_marketing_actions = len(pending_marketing),
        pending_content_posts     = len(pending_content),
        open_return_insights      = len(open_return_insights),
        recent_runs               = [RunSummarySchema.model_validate(r) for r in recent_runs],
        critical_alerts           = [AlertSchema.model_validate(a) for a in critical_alerts],
    )