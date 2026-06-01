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
    units_per_day:          float
    days_of_stock_remaining: float
    urgency:                str   # "critical" | "high" | "normal" | "healthy"


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
    action:           str   # "hold" | "increase" | "markdown" | "bundle"
    discount_pct:     float
    reason:           str


class RestockRecommendation(TypedDict):
    sku:                      str
    recommended_quantity:     int
    urgency:                  str
    days_of_stock_remaining:  float
    units_per_day:            float
    reason:                   str
    supplier_message:         str
    status:                   str  # "pending_approval" | "approved" | "ordered"


class MarketingAction(TypedDict):
    sku:          str
    action:       str   # "increase_budget" | "decrease_budget" | "pause" | "activate"
    reason:       str
    amount_delta: Optional[float]  # Budget change amount


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
    content_queue:              Annotated[list[dict], operator.add]  # Posts to schedule
    dm_replies:                 Annotated[list[dict], operator.add]  # Auto-replies sent
    alerts:                     Annotated[list[AgentAlert], operator.add]

    # ── Supervisor routing ────────────────────────────────────────────────────
    agents_to_run:       list[str]   # Which agents the supervisor decided to activate
    completed_agents:    list[str]   # Which agents have finished
    next_agent:          Optional[str]
    supervisor_reasoning: str        # Why the supervisor made these routing decisions

    # ── Final summary ─────────────────────────────────────────────────────────
    run_summary: Optional[str]   # Human-readable summary of what happened this run
    completed_at: Optional[str]