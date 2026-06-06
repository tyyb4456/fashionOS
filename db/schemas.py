"""
FashionOS API Schemas
======================
Pydantic models for FastAPI request/response serialization.

These mirror the SQLAlchemy models but are decoupled — the ORM models
are the source of truth for the DB schema, the Pydantic schemas are
the contract for the API. Changes to one don't automatically affect the other.

Used as response_model= types in api/routers/ (when built):
  GET /api/v1/runs               → list[RunSummarySchema]
  GET /api/v1/runs/{run_id}      → RunDetailSchema
  GET /api/v1/alerts/critical    → list[AlertSchema]
  GET /api/v1/pricing/pending    → list[PricingActionSchema]
  GET /api/v1/restock/pending    → list[RestockRecommendationSchema]
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ── Base config ───────────────────────────────────────────────────────────────

class _Base(BaseModel):
    # from_attributes = True lets Pydantic read SQLAlchemy ORM objects directly
    model_config = ConfigDict(from_attributes=True)


# ══════════════════════════════════════════════════════════════════════════════
# Child schemas
# ══════════════════════════════════════════════════════════════════════════════

class InventorySnapshotSchema(_Base):
    id:                      UUID
    run_id:                  str
    brand_id:                str
    sku:                     str
    product_title:           str
    variant_title:           str
    current_stock:           int
    units_per_day:           float
    days_of_stock_remaining: float
    urgency:                 str
    created_at:              datetime


class PricingActionSchema(_Base):
    id:                UUID
    run_id:            str
    brand_id:          str
    sku:               str
    variant_id:        Optional[int]
    action:            str
    current_price:     float
    recommended_price: float
    discount_pct:      float
    auto_executed:     bool
    reason:            Optional[str]
    created_at:        datetime


class AlertSchema(_Base):
    id:         UUID
    run_id:     str
    brand_id:   str
    level:      str    # "critical" | "warning" | "info"
    agent:      str
    message:    str
    sku:        Optional[str]
    created_at: datetime


class RestockRecommendationSchema(_Base):
    id:                      UUID
    run_id:                  str
    brand_id:                str
    sku:                     str
    recommended_quantity:    int
    urgency:                 str
    days_of_stock_remaining: float
    units_per_day:           float
    reason:                  str
    supplier_message:        str
    status:                  str
    created_at:              datetime


# ══════════════════════════════════════════════════════════════════════════════
# Run schemas
# ══════════════════════════════════════════════════════════════════════════════

class RunSummarySchema(_Base):
    """
    Lightweight — returned in the run history list.
    Contains cached aggregate counts; no child table JOINs needed.
    """
    id:                      UUID
    run_id:                  str
    brand_id:                str
    brand_name:              str
    trigger:                 str
    task_id:                 Optional[str]
    started_at:              datetime
    completed_at:            Optional[datetime]
    agents_run:              Optional[Any]   # list[str]
    run_summary:             Optional[str]
    alert_count_critical:    int
    alert_count_warning:     int
    alert_count_total:       int
    inventory_skus_analysed: int
    pricing_decisions_total: int
    pricing_auto_executed:   int
    pricing_pending_approval: int
    created_at:              datetime


class RunDetailSchema(RunSummarySchema):
    """
    Full detail — returned on the run detail page.
    Includes supervisor reasoning and all child records.
    Note: child lists are populated by the route handler (separate queries),
    not by a JOIN — avoids loading thousands of rows in a single query.
    """
    trigger_payload:      Optional[Any]    # dict
    supervisor_reasoning: Optional[str]
    inventory_snapshots:  list[InventorySnapshotSchema] = []
    pricing_actions:      list[PricingActionSchema]     = []
    alerts:               list[AlertSchema]             = []


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard widget schemas
# ══════════════════════════════════════════════════════════════════════════════

class DashboardSummarySchema(BaseModel):
    """
    Single-object payload for the dashboard home screen.
    Aggregates data from the last N runs without requiring the frontend
    to make multiple API calls.
    """
    brand_id:                  str
    last_run_at:               Optional[datetime]
    last_run_summary:          Optional[str]
    total_runs_today:          int
    critical_alerts_open:      int
    pending_pricing_decisions: int
    pending_restock_orders:    int
    recent_runs:               list[RunSummarySchema] = []
    critical_alerts:           list[AlertSchema]      = []