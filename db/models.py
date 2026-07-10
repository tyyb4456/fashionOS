"""
FashionOS Database Models
=========================
SQLAlchemy 2.0 ORM models. One row-set per agent run.

Tables:
  agent_runs                 ← One row per supervisor run (top-level record)
  inventory_snapshots        ← Per-SKU snapshot written by Inventory Agent
  pricing_actions            ← Per-SKU decision written by Pricing Agent
  alerts                     ← All agent alerts (merged from all agents)
  restock_recommendations    ← Pending restock orders
  marketing_actions          ← Per-campaign budget decisions (NEW session 6)
  content_posts              ← Generated Instagram + TikTok content (NEW session 6)
  return_insights            ← Structured return patterns (NEW session 6)

Session 6 additions:
  - MarketingActionRecord  — persists Marketing Agent campaign decisions
  - ContentPostRecord      — persists Content Agent content_queue items
  - ReturnInsightRecord    — persists Returns Agent structured patterns
  - AgentRun gains 3 new cached aggregate columns for marketing stats
"""

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# agent_runs  — top-level run record
# ══════════════════════════════════════════════════════════════════════════════

class AgentRun(Base):
    """One row per supervisor pipeline invocation."""
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)

    brand_id:   Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False)

    trigger:         Mapped[str]            = mapped_column(String(50), nullable=False)
    trigger_payload: Mapped[Optional[Any]]  = mapped_column(JSON, nullable=True)
    task_id:         Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)

    started_at:   Mapped[datetime]           = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agents_run:           Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    run_summary:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supervisor_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Cached aggregate counts — fast dashboard list view without JOINs
    alert_count_critical:     Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_count_warning:      Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_count_total:        Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    inventory_skus_analysed:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    pricing_decisions_total:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pricing_auto_executed:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pricing_pending_approval: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Marketing cached counts (NEW session 6)
    marketing_decisions_total:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marketing_auto_executed:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marketing_pending_approval: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# inventory_snapshots
# ══════════════════════════════════════════════════════════════════════════════

class InventorySnapshotRecord(Base):
    __tablename__ = "inventory_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku:           Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    variant_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    current_stock:           Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    units_per_day:           Mapped[float] = mapped_column(Float,   nullable=False, default=0.0)
    days_of_stock_remaining: Mapped[float] = mapped_column(Float,   nullable=False, default=999.0)

    urgency: Mapped[str] = mapped_column(String(20), nullable=False, default="healthy")

    # NEW — seasonal/trend-aware Inventory Agent
    velocity_7d:         Mapped[float] = mapped_column(Float,      nullable=False, default=0.0)
    velocity_30d:        Mapped[float] = mapped_column(Float,      nullable=False, default=0.0)
    velocity_trend:      Mapped[str]   = mapped_column(String(20), nullable=False, default="stable")
    velocity_confidence: Mapped[str]   = mapped_column(String(10), nullable=False, default="low")

    seasonal_multiplier_applied:        Mapped[float] = mapped_column(Float,      nullable=False, default=1.0)
    seasonal_context:                   Mapped[str]   = mapped_column(String(50), nullable=False, default="off_season")
    days_of_stock_remaining_unadjusted: Mapped[float] = mapped_column(Float,      nullable=False, default=999.0)

    reorder_point_units:  Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    has_pending_restock:  Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    pending_restock_note: Mapped[Optional[str]] = mapped_column(Text,    nullable=True)

    size_curve_deviation: Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    size_curve_note:      Mapped[Optional[str]] = mapped_column(Text,    nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

# ══════════════════════════════════════════════════════════════════════════════
# pricing_actions
# ══════════════════════════════════════════════════════════════════════════════

class PricingActionRecord(Base):
    __tablename__ = "pricing_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku:        Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    variant_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    action:            Mapped[str]   = mapped_column(String(30),  nullable=False)
    current_price:     Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    recommended_price: Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    discount_pct:      Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    auto_executed:     Mapped[bool]  = mapped_column(Boolean,     nullable=False, default=False)
    reason:            Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # NEW — deterministic pricing intelligence
    trigger:                  Mapped[str]              = mapped_column(String(50), nullable=False, default="healthy")
    markdown_rung:             Mapped[int]               = mapped_column(Integer, nullable=False, default=0)
    estimated_unit_cost_pkr:    Mapped[Optional[float]]   = mapped_column(Float, nullable=True)
    estimated_margin_pct:       Mapped[Optional[float]]   = mapped_column(Float, nullable=True)
    suggested_discount_code:    Mapped[Optional[str]]     = mapped_column(String(100), nullable=True)
    new_compare_at_price:        Mapped[Optional[float]]   = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# alerts
# ══════════════════════════════════════════════════════════════════════════════

class AlertRecord(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    level: Mapped[str] = mapped_column(String(20),  nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(100), nullable=False)

    message: Mapped[str]           = mapped_column(Text,       nullable=False)
    sku:     Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# restock_recommendations
# ══════════════════════════════════════════════════════════════════════════════

class RestockRecommendationRecord(Base):
    __tablename__ = "restock_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    recommended_quantity:    Mapped[int]   = mapped_column(Integer,    nullable=False, default=0)
    urgency:                 Mapped[str]   = mapped_column(String(20), nullable=False)
    days_of_stock_remaining: Mapped[float] = mapped_column(Float,      nullable=False)
    units_per_day:           Mapped[float] = mapped_column(Float,      nullable=False)

    reason:           Mapped[str] = mapped_column(Text, nullable=False, default="")
    supplier_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending_approval")

    # NEW — deterministic restock intelligence
    supplier_type:            Mapped[str]             = mapped_column(String(30), nullable=False, default="lahore_local")
    estimated_lead_days:      Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    expected_stockout_date:   Mapped[Optional[date]]   = mapped_column(Date, nullable=True)
    order_deadline:           Mapped[Optional[date]]   = mapped_column(Date, nullable=True)
    is_overdue:                Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    estimated_unit_cost_pkr:   Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    estimated_total_cost_pkr:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    priority:                  Mapped[int]              = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# marketing_actions  — NEW session 6
# ══════════════════════════════════════════════════════════════════════════════

class MarketingActionRecord(Base):
    """
    One row per Meta campaign per run.
    Persists the full decision including auto-executed budget changes
    and pending-approval increases. Enables the dashboard's Marketing tab:
    campaign spend history, budget change log, pending approvals.
    """
    __tablename__ = "marketing_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Campaign identity
    sku:           Mapped[Optional[str]] = mapped_column(String(255), nullable=True,  index=True)
    campaign_id:   Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    campaign_name: Mapped[str]           = mapped_column(String(500), nullable=False)

    # Decision
    # "hold" | "increase_budget" | "decrease_budget" | "pause" | "activate"
    action:              Mapped[str]           = mapped_column(String(50),  nullable=False)
    current_budget_pkr:  Mapped[float]         = mapped_column(Float,       nullable=False, default=0.0)
    new_budget_pkr:      Mapped[Optional[float]]= mapped_column(Float,      nullable=True)
    change_pct:          Mapped[float]         = mapped_column(Float,       nullable=False, default=0.0)
    auto_executed:       Mapped[bool]          = mapped_column(Boolean,     nullable=False, default=False)
    reason:              Mapped[Optional[str]] = mapped_column(Text,        nullable=True)

    # What triggered the decision
    # "out_of_stock" | "clearance" | "trending" | "organic_viral" | "low_roas" | "healthy" | "no_sku_match"
    trigger: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # NEW — deterministic marketing intelligence
    roas_7d:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spend_7d_pkr:  Mapped[float]           = mapped_column(Float, nullable=False, default=0.0)
    ctr_7d:         Mapped[float]           = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# content_posts  — NEW session 6
# ══════════════════════════════════════════════════════════════════════════════

class ContentPostRecord(Base):
    """
    One row per generated content piece per run.
    Persists content_queue from the Content Agent so the dashboard can
    show a "Content Calendar" view with Instagram + TikTok copy ready to use.
    status starts as "pending" — updated to "posted" or "skipped" via the dashboard.
    """
    __tablename__ = "content_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku:           Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    variant_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    is_urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # "pending" | "posted" | "skipped"
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    # Instagram content
    instagram_caption:   Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)
    instagram_hashtags:  Mapped[Optional[Any]]  = mapped_column(JSON,        nullable=True)  # list[str]
    instagram_post_time: Mapped[Optional[str]]  = mapped_column(String(50),  nullable=True)

    # TikTok content (stored as JSON to keep the 4-part structure)
    tiktok_script:    Mapped[Optional[Any]] = mapped_column(JSON,       nullable=True)  # {hook, context, reveal, cta}
    tiktok_post_time: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Metadata
    creator_notes: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    sale_mention:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# return_insights  — NEW session 6
# ══════════════════════════════════════════════════════════════════════════════

class ReturnInsightRecord(Base):
    """
    One row per flagged SKU per run from the Returns Agent.
    Persists structured return patterns — enabling a "Returns Fix Queue"
    dashboard table sorted by severity with fix_type filters.

    Previously only alerts were written (text blobs). This table adds the
    structured form so the dashboard can show: SKU, rate, reason, fix action.
    healthy SKUs (< 3 returns, < 5% rate) are not persisted — noise reduction.
    """
    __tablename__ = "return_insights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id:   Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku:           Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    total_returns:        Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_units_returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # see fashion_returns skill taxonomy
    primary_reason: Mapped[str]           = mapped_column(String(100), nullable=False)
    return_rate_pct: Mapped[Optional[float]] = mapped_column(Float,    nullable=True)
    estimated_30d_sales: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # "critical" | "warning" | "info"
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    recommended_fix: Mapped[str]           = mapped_column(Text,       nullable=False, default="")
    # "update_size_guide" | "update_photos" | "update_description" |
    # "quality_review" | "contact_supplier" | "monitor" | "no_action"
    fix_type:        Mapped[str]           = mapped_column(String(100), nullable=False, default="monitor")

    # NEW — was computed before but silently dropped before persistence
    reason_breakdown: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)  # dict[str, int]
    evidence:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# chat_tool_results  — persists structured tool output per chat turn
# ══════════════════════════════════════════════════════════════════════════════

class ChatToolResult(Base):
    """
    One row per tool call (or persisted reasoning block) during a chat turn.

    Keyed by (brand_id, thread_id, turn_index, label).
    turn_index = 0-based index of the assistant message this belongs to,
    computed by counting existing AI messages in the checkpoint at stream start.

    data column stores the full structured JSON (InventoryAnalysis, etc.)
    so the frontend can render rich cards when loading conversation history.
    """
    __tablename__ = "chat_tool_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    brand_id:  Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Human-readable label for this row: a tool name (get_inventory_status),
    # a comma-joined list of pipeline agents that ran (inventory,trend,pricing),
    # or the reasoning sentinel (see deep_agents/streaming.py REASONING_SENTINEL).
    label: Mapped[str] = mapped_column(String(100), nullable=False)

    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data:    Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# brands  — tenant registry
# ══════════════════════════════════════════════════════════════════════════════

class Brand(Base):
    __tablename__ = "brands"

    id:         Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id:   Mapped[str]       = mapped_column(String(100), unique=True, nullable=False, index=True)
    brand_name: Mapped[str]       = mapped_column(String(255), nullable=False)
    owner_email:Mapped[str]       = mapped_column(String(255), unique=True, nullable=False, index=True)
    clerk_user_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    plan:       Mapped[str]       = mapped_column(String(50),  nullable=False, default="starter")
    is_active:  Mapped[bool]      = mapped_column(Boolean,     nullable=False, default=True)

    # Shopify
    shopify_shop_name:          Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    shopify_access_token_enc:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shopify_webhook_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Meta Ads
    meta_access_token_enc: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    meta_ad_account_id:    Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Instagram DMs
    instagram_access_token_enc: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    instagram_page_id:          Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Notification recipients (WHERE to send — brand owner's contacts)
    brand_owner_whatsapp: Mapped[Optional[str]] = mapped_column(String(50),  nullable=True)
    brand_owner_email:    Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)



# ══════════════════════════════════════════════════════════════════════════════
# api_keys  — one or more per brand
# ══════════════════════════════════════════════════════════════════════════════

class ApiKey(Base):
    """
    API key for a brand. The plaintext key is shown once on creation;
    only the SHA-256 hash is persisted here.

    key_prefix: first portion (e.g. "fos_mybrand") stored for fast lookup.
    key_hash:   sha256(full_key) — compared on every request.
    """
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    brand_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    key_prefix:   Mapped[str]           = mapped_column(String(20),  nullable=False)
    key_hash:     Mapped[str]           = mapped_column(String(64),  nullable=False, unique=True, index=True)
    label:        Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active:    Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )