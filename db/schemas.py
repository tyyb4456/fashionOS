"""
FashionOS API Schemas
======================
Pydantic models for FastAPI request/response serialization.

Session 6 additions:
  - MarketingActionSchema
  - ContentPostSchema
  - ReturnInsightSchema
  - RunSummarySchema gains marketing cached counts
  - DashboardSummarySchema gains pending_marketing_actions + return_insights counts
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ══════════════════════════════════════════════════════════════════════════════
# Existing child schemas (unchanged)
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
    level:      str
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
# NEW — Session 6
# ══════════════════════════════════════════════════════════════════════════════

class MarketingActionSchema(_Base):
    """
    Per-campaign budget decision from the Marketing Agent.
    auto_executed=True  → already applied via Meta API.
    auto_executed=False → pending in dashboard for human approval (budget increase / activate).
    """
    id:                  UUID
    run_id:              str
    brand_id:            str
    sku:                 Optional[str]
    campaign_id:         str
    campaign_name:       str
    action:              str    # "hold" | "increase_budget" | "decrease_budget" | "pause" | "activate"
    current_budget_pkr:  float
    new_budget_pkr:      Optional[float]
    change_pct:          float
    auto_executed:       bool
    reason:              Optional[str]
    trigger:             Optional[str]
    created_at:          datetime


class ContentPostSchema(_Base):
    """
    One generated content piece (Instagram + TikTok) from the Content Agent.
    status starts as "pending" — updated via dashboard when posted/skipped.
    """
    id:                  UUID
    run_id:              str
    brand_id:            str
    sku:                 str
    product_title:       str
    variant_title:       str
    is_urgent:           bool
    status:              str    # "pending" | "posted" | "skipped"
    instagram_caption:   Optional[str]
    instagram_hashtags:  Optional[Any]   # list[str]
    instagram_post_time: Optional[str]
    tiktok_script:       Optional[Any]   # {hook, context, reveal, cta}
    tiktok_post_time:    Optional[str]
    creator_notes:       Optional[str]
    sale_mention:        Optional[str]
    created_at:          datetime


class ReturnInsightSchema(_Base):
    """
    Structured return pattern for one SKU.
    Only critical/warning/info severity rows are stored (healthy = no record).
    """
    id:                   UUID
    run_id:               str
    brand_id:             str
    sku:                  str
    product_title:        str
    total_returns:        int
    total_units_returned: int
    primary_reason:       str
    return_rate_pct:      Optional[float]
    estimated_30d_sales:  Optional[int]
    severity:             str    # "critical" | "warning" | "info"
    recommended_fix:      str
    fix_type:             str
    created_at:           datetime


# ══════════════════════════════════════════════════════════════════════════════
# Run schemas — updated
# ══════════════════════════════════════════════════════════════════════════════

class RunSummarySchema(_Base):
    """Lightweight — returned in the run history list."""
    id:                      UUID
    run_id:                  str
    brand_id:                str
    brand_name:              str
    trigger:                 str
    task_id:                 Optional[str]
    started_at:              datetime
    completed_at:            Optional[datetime]
    agents_run:              Optional[Any]
    run_summary:             Optional[str]
    alert_count_critical:    int
    alert_count_warning:     int
    alert_count_total:       int
    inventory_skus_analysed: int
    pricing_decisions_total: int
    pricing_auto_executed:   int
    pricing_pending_approval: int
    # Marketing (NEW session 6)
    marketing_decisions_total:  int = 0
    marketing_auto_executed:    int = 0
    marketing_pending_approval: int = 0
    created_at:              datetime


class RunDetailSchema(RunSummarySchema):
    """Full detail — returned on the run detail page."""
    trigger_payload:      Optional[Any]
    supervisor_reasoning: Optional[str]
    inventory_snapshots:  list[InventorySnapshotSchema] = []
    pricing_actions:      list[PricingActionSchema]     = []
    alerts:               list[AlertSchema]             = []
    marketing_actions:    list[MarketingActionSchema]   = []   # NEW
    content_posts:        list[ContentPostSchema]       = []   # NEW
    return_insights:      list[ReturnInsightSchema]     = []   # NEW


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard widget schemas — updated
# ══════════════════════════════════════════════════════════════════════════════

class DashboardSummarySchema(BaseModel):
    """Single-object payload for the dashboard home screen."""
    brand_id:                  str
    last_run_at:               Optional[datetime]
    last_run_summary:          Optional[str]
    total_runs_today:          int
    critical_alerts_open:      int
    pending_pricing_decisions: int
    pending_restock_orders:    int
    # NEW session 6
    pending_marketing_actions: int = 0
    pending_content_posts:     int = 0
    open_return_insights:      int = 0
    recent_runs:               list[RunSummarySchema] = []
    critical_alerts:           list[AlertSchema]      = []