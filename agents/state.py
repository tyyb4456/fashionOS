"""
FashionOS Shared State
The single TypedDict that flows through the entire LangGraph supervisor graph.
Every agent reads from this and writes its outputs back to it.
"""

from typing import TypedDict, Optional, Annotated
from datetime import datetime
import operator


# ── Sub-schemas ───────────────────────────────────────────────────────────────

class InventorySnapshot(TypedDict):
    sku:                    str
    product_title:          str
    variant_title:          str
    current_stock:          int
    units_per_day:          float    # 7-day velocity (was a flat 14d average)
    days_of_stock_remaining: float   # seasonally-adjusted forecast
    urgency:                str      # "critical" | "high" | "normal" | "healthy"

    # NEW — seasonal/trend-aware Inventory Agent
    velocity_7d:                        float
    velocity_30d:                       float
    velocity_trend:                     str    # "accelerating" | "stable" | "decelerating" | "new_item" | "no_movement"
    velocity_confidence:                str    # "high" | "medium" | "low"
    seasonal_multiplier_applied:        float
    seasonal_context:                   str
    days_of_stock_remaining_unadjusted: float
    reorder_point_units:                int
    has_pending_restock:                bool
    pending_restock_note:               Optional[str]
    size_curve_deviation:                bool
    size_curve_note:                     Optional[str]


class TrendSignal(TypedDict):
    keyword:    str
    platform:   str           # "tiktok" | "instagram" | "google_trends"
    score:      float         # 0.0 – 1.0, relative trend strength
    direction:  str           # "rising" | "peaking" | "declining"
    matched_sku: Optional[str] # SKU in the store that matches this trend


class PricingRecommendation(TypedDict):
    sku:              str
    variant_id:       int
    current_price:    float
    recommended_price: float
    action:           str   # "hold" | "increase" | "markdown" | "clearance_code"
    discount_pct:     float
    reason:           str

    # NEW — deterministic pricing intelligence
    auto_executed:            bool
    trigger:                   str      # see agents/pricing/graph.py _HOLD_REASON_BY_TRIGGER for the full set
    markdown_rung:              int      # rung AFTER this decision (0=full price, 1≈15%, 2≈25%, 3=clearance)
    estimated_unit_cost_pkr:    Optional[float]
    estimated_margin_pct:       Optional[float]
    suggested_discount_code:    Optional[str]
    new_compare_at_price:        Optional[float]

class RestockRecommendation(TypedDict):
    sku:                      str
    recommended_quantity:     int
    urgency:                  str
    days_of_stock_remaining:  float
    units_per_day:            float
    reason:                   str
    supplier_message:         str
    status:                   str  # "pending_approval" | "approved" | "ordered"

    # NEW — deterministic restock intelligence
    supplier_type:            str      # "lahore_local" | "karachi_trader" | "china_import"
    estimated_lead_days:      int
    expected_stockout_date:   str      # ISO date
    order_deadline:           str      # ISO date — latest date the PO can be placed without a gap
    is_overdue:                bool
    estimated_unit_cost_pkr:   Optional[float]
    estimated_total_cost_pkr:  Optional[float]
    priority:                  int     # 1 = highest, assigned after sort (overdue → critical → stockout date)


class MarketingAction(TypedDict):
    """One budget/status decision per Meta campaign."""
    sku:           str
    campaign_id:   str
    campaign_name: str
    action:        str   # "hold" | "increase_budget" | "decrease_budget" | "pause" | "activate"
    reason:        str
    auto_executed: bool
    trigger:       str   # "no_sku_match" | "no_budget_control" | "out_of_stock" | "clearance" |
                          # "trending_increase" | "trending_hold_low_roas" | "organic_viral" |
                          # "low_roas_pause" | "low_roas_decrease" | "healthy"

    # NEW — deterministic marketing intelligence (replaces amount_delta)
    current_budget_pkr: float
    new_budget_pkr:      Optional[float]
    change_pct:            float
    roas_7d:                Optional[float]
    spend_7d_pkr:            float
    ctr_7d:                   float


class ReturnInsightData(TypedDict):
    """
    Structured return pattern for one SKU — written by Returns Agent in session 6.
    Enables DB persistence in return_insights table and structured dashboard display.
    Previously, only alerts were written (text-only); this adds the structured form.
    """
    sku:                   str
    product_title:         str
    total_returns:         int
    total_units_returned:  int
    primary_reason:        str   # see fashion_returns skill for taxonomy
    return_rate_pct:       Optional[float]
    estimated_30d_sales:   Optional[int]
    severity:              str   # "critical" | "warning" | "info"
    recommended_fix:       str
    fix_type:              str   # "update_size_guide" | "update_photos" | etc.


class DMReply(TypedDict):
    """
    One processed DM per run — written by the DM Agent. Classification (LLM,
    Node 2) and gating (Python fixed lookup, Node 3) are computed separately;
    this is the merged final record persisted to the dm_replies table.
    """
    message_id:       str
    conversation_id:  str
    user_id:          str
    username:         str
    original_message: str  # truncated to 200 chars

    category: str   # "size_question" | "availability" | "order_status" | "general_inquiry" |
                     # "bulk_inquiry" | "complaint" | "influencer" | "spam"

    auto_send:      bool
    flag_for_human: bool
    flag_priority:  Optional[str]   # "high" | "normal" | None
    flag_reason:    Optional[str]

    reply_text: Optional[str]
    auto_sent:  bool
    sent_at:    Optional[str]

    status: str   # "auto_sent" | "send_failed" | "flagged_open" | "flagged_resolved"


class ContentQueueItem(TypedDict):
    """
    One generated content piece (Instagram + TikTok) — written by the Content
    Agent. Posting times, hashtags, sale_mention, trigger, and is_urgent are
    all computed deterministically (Node 2 — compute_content_plan). Only the
    nested caption/script text and creator_notes come from the LLM (Node 3).
    """
    sku:           str
    product_title: str
    variant_title: str

    is_urgent:    bool
    trigger:      str             # "trending" | "on_sale"
    trend_score:  Optional[float] # None unless trigger == "trending"
    discount_pct: float

    status:     str   # "pending" | "posted" | "skipped"
    created_at: str

    instagram: dict   # {"caption": str, "hashtags": list[str], "optimal_time": str}
    tiktok:    dict   # {"script": {"hook","context","reveal","cta"}, "optimal_time": str}

    creator_notes: str
    sale_mention:  Optional[str]


class AgentAlert(TypedDict):
    level:      str    # "critical" | "warning" | "info"
    agent:      str    # which agent raised this
    message:    str
    sku:        Optional[str]
    created_at: str


# ── Main shared state ─────────────────────────────────────────────────────────

class FashionOSState(TypedDict):

    # ── Identity ──────────────────────────────────────────────────────────────
    brand_id:   str    # Unique identifier for the brand (multi-tenancy)
    brand_name: str

    # ── Trigger context ───────────────────────────────────────────────────────
    trigger:         str   # "shopify_webhook" | "scheduled_run" | "manual"
    trigger_payload: dict  # Raw payload (webhook body, schedule config, etc.)
    run_id:          str   # Unique ID for this agent run (for logging)
    started_at:      str   # ISO timestamp when this run started

    # ── Live data (populated by data-fetching nodes before agents run) ────────
    products:          list[dict]   # Raw Shopify product list
    recent_orders:     list[dict]   # Orders from last 24h
    sales_velocity:    list[dict]   # Units/day per SKU
    inventory_snapshot: list[InventorySnapshot]

    # ── Agent outputs (each agent appends to these lists) ─────────────────────
    # Using Annotated[list, operator.add] tells LangGraph to MERGE these
    # instead of replacing them when agents write to state in parallel.
    trend_signals:              Annotated[list[TrendSignal], operator.add]
    pricing_recommendations:    Annotated[list[PricingRecommendation], operator.add]
    restock_recommendations:    Annotated[list[RestockRecommendation], operator.add]
    marketing_actions:          Annotated[list[MarketingAction], operator.add]
    content_queue:              Annotated[list[ContentQueueItem], operator.add]  # Posts to schedule
    dm_replies:                 Annotated[list[DMReply], operator.add]  # Auto-replies + flagged DMs
    alerts:                     Annotated[list[AgentAlert], operator.add]

    # ── Returns structured output (session 6 — alongside alerts) ──────────────
    # Returns Agent now writes both:
    #   state.alerts         → text alerts surfaced in dashboard notifications
    #   state.return_insights → structured data persisted in return_insights table
    return_insights: Annotated[list[ReturnInsightData], operator.add]

    # ── Supervisor routing ────────────────────────────────────────────────────
    agents_to_run:       list[str]   # Which agents the supervisor decided to activate
    completed_agents:    list[str]   # Which agents have finished
    next_agent:          Optional[str]
    supervisor_reasoning: str        # Why the supervisor made these routing decisions

    # ── Final summary ─────────────────────────────────────────────────────────
    run_summary: Optional[str]   # Human-readable summary of what happened this run
    completed_at: Optional[str]