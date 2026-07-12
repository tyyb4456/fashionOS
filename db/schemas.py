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

from datetime import date, datetime
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

    # NEW — seasonal/trend-aware Inventory Agent
    velocity_7d:                        float = 0.0
    velocity_30d:                       float = 0.0
    velocity_trend:                     str   = "stable"
    velocity_confidence:                str   = "low"
    seasonal_multiplier_applied:        float = 1.0
    seasonal_context:                   str   = "off_season"
    days_of_stock_remaining_unadjusted: float = 999.0
    reorder_point_units:                int   = 0
    has_pending_restock:                bool  = False
    pending_restock_note:               Optional[str] = None
    size_curve_deviation:               bool  = False
    size_curve_note:                    Optional[str] = None

    created_at: datetime


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

    # NEW
    trigger:                  str = "healthy"
    markdown_rung:              int = 0
    estimated_unit_cost_pkr:    Optional[float] = None
    estimated_margin_pct:       Optional[float] = None
    suggested_discount_code:    Optional[str] = None
    new_compare_at_price:        Optional[float] = None
    
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

    # NEW
    supplier_type:            str = "lahore_local"
    estimated_lead_days:      int = 0
    expected_stockout_date:   Optional[date] = None
    order_deadline:           Optional[date] = None
    is_overdue:                bool = False
    estimated_unit_cost_pkr:   Optional[float] = None
    estimated_total_cost_pkr:  Optional[float] = None
    priority:                  int = 0
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

    # NEW
    roas_7d:      Optional[float] = None
    spend_7d_pkr:  float = 0.0
    ctr_7d:         float = 0.0
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

    # NEW
    trigger:      str = "on_sale"   # "trending" | "on_sale"
    trend_score:  Optional[float] = None
    discount_pct: float = 0.0

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
    fix_type:              str

    # NEW
    reason_breakdown: Optional[dict[str, int]] = None
    evidence:          Optional[str] = None
    created_at:           datetime


class DMReplySchema(_Base):
    """
    One processed DM per run. status: "auto_sent" | "send_failed" |
    "flagged_open" | "flagged_resolved". Spam is never persisted.
    """
    id:                UUID
    run_id:             str
    brand_id:           str
    message_id:         str
    conversation_id:    str
    user_id:            str
    username:           str
    original_message:   str
    category:           str
    auto_send:          bool
    flag_for_human:     bool
    flag_priority:      Optional[str]
    flag_reason:        Optional[str]
    reply_text:         Optional[str]
    auto_sent:          bool
    sent_at:            Optional[datetime]
    status:             str
    created_at:         datetime



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
    # DM
    dm_auto_replied: int = 0
    dm_flagged_open: int = 0
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
    dm_replies:            list[DMReplySchema]           = []   # NEW


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard widget schemas — updated
# ══════════════════════════════════════════════════════════════════════════════

class SeasonalContextSchema(BaseModel):
    """
    Mirrors agents.seasonal.current_seasonal_context(). Computed fresh on every
    dashboard request — not stored, not tied to a specific run — so it's always
    accurate to "today," independent of when the last agent run happened.
    """
    season_label:         str
    demand_multiplier:    float
    days_until_next_peak: Optional[int]  = None
    next_peak_label:      Optional[str]  = None
    next_peak_confirmed:  Optional[bool] = None


class DashboardSummarySchema(BaseModel):
    """Single-object payload for the dashboard home screen."""
    brand_id:                  str
    last_run_at:               Optional[datetime]
    last_run_summary:          Optional[str]
    total_runs_today:          int
    critical_alerts_open:      int
    pending_pricing_decisions: int
    pending_restock_orders:    int
    pending_marketing_actions: int = 0
    pending_content_posts:     int = 0
    open_return_insights:      int = 0
    open_flagged_dms:          int = 0
    seasonal_context:          SeasonalContextSchema   # NEW
    recent_runs:                list[RunSummarySchema] = []
    critical_alerts:            list[AlertSchema]      = []