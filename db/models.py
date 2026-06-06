"""
FashionOS Database Models
=========================
SQLAlchemy 2.0 ORM models. One row-set per agent run.

Tables:
  agent_runs                 ← One row per supervisor run (the top-level record)
  inventory_snapshots        ← Per-SKU snapshot written by Inventory Agent
  pricing_actions            ← Per-SKU decision written by Pricing Agent
  alerts                     ← All agent alerts (merged from all agents)
  restock_recommendations    ← Pending restock orders (pre-wired for Restock Agent)

Design decisions:
  - UUID primary keys on all tables — safe for distributed workers, no hot spots.
  - `run_id` (string, from FashionOSState) is the join key across all tables.
    Kept as VARCHAR(36) so it's human-readable in logs and matches the state field type.
  - Cached aggregate counts on `agent_runs` (alert_count_*, pricing_*) mean the
    dashboard list view never needs to JOIN — cheap to render run history at scale.
  - `restock_recommendations` table is pre-created now even though the Restock Agent
    isn't built yet — schema change without data migration later.
  - No FK constraints in DB — run_id is the logical FK but we skip the physical one
    to avoid cascade complexity during development. Add in production if needed.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# agent_runs  — top-level run record
# ══════════════════════════════════════════════════════════════════════════════

class AgentRun(Base):
    """
    One row per supervisor pipeline invocation.

    Contains cached aggregate counts so dashboard queries never need to JOIN.
    Full per-SKU data lives in the child tables below.
    """
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # run_id comes from FashionOSState — a UUID string generated at pipeline start.
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    brand_id:   Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ── Trigger context ───────────────────────────────────────────────────────
    trigger:         Mapped[str]            = mapped_column(String(50), nullable=False)
    trigger_payload: Mapped[Optional[Any]]  = mapped_column(JSON, nullable=True)
    task_id:         Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)

    # ── Timing ────────────────────────────────────────────────────────────────
    started_at:   Mapped[datetime]           = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Run metadata ──────────────────────────────────────────────────────────
    agents_run:           Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)    # list[str]
    run_summary:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supervisor_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Cached aggregate counts (for fast dashboard list view) ─────────────────
    alert_count_critical:     Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_count_warning:      Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_count_total:        Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    inventory_skus_analysed:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    pricing_decisions_total:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pricing_auto_executed:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pricing_pending_approval: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Row timestamp ─────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# inventory_snapshots  — per-SKU row written by Inventory Agent
# ══════════════════════════════════════════════════════════════════════════════

class InventorySnapshotRecord(Base):
    """
    One row per SKU per run. Maps to FashionOSState.inventory_snapshot[].
    Sorted by days_of_stock_remaining for display (most urgent first).
    """
    __tablename__ = "inventory_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id:   Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # ── SKU data ──────────────────────────────────────────────────────────────
    sku:           Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    variant_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # ── Stock metrics ─────────────────────────────────────────────────────────
    current_stock:           Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    units_per_day:           Mapped[float] = mapped_column(Float,   nullable=False, default=0.0)
    days_of_stock_remaining: Mapped[float] = mapped_column(Float,   nullable=False, default=999.0)

    # ── Classification ────────────────────────────────────────────────────────
    # "critical" | "high" | "normal" | "healthy"
    urgency: Mapped[str] = mapped_column(String(20), nullable=False, default="healthy")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# pricing_actions  — per-SKU decision written by Pricing Agent
# ══════════════════════════════════════════════════════════════════════════════

class PricingActionRecord(Base):
    """
    One row per SKU per run, including "hold" decisions.
    auto_executed=True  → already applied in Shopify via MCP.
    auto_executed=False + action != 'hold' → pending in dashboard for human approval.
    """
    __tablename__ = "pricing_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id:   Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # ── SKU identity ──────────────────────────────────────────────────────────
    sku:        Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    variant_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # ── Decision ──────────────────────────────────────────────────────────────
    # "hold" | "markdown" | "increase" | "clearance_code" | "bundle"
    action:            Mapped[str]   = mapped_column(String(30),  nullable=False)
    current_price:     Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    recommended_price: Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    discount_pct:      Mapped[float] = mapped_column(Float,       nullable=False, default=0.0)
    auto_executed:     Mapped[bool]  = mapped_column(Boolean,     nullable=False, default=False)
    reason:            Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# alerts  — all agent alerts merged per run
# ══════════════════════════════════════════════════════════════════════════════

class AlertRecord(Base):
    """
    All alerts raised by any agent during a run.
    Uses the created_at from the alert itself (not server default) to preserve
    the original agent timestamp.
    """
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id:   Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # "critical" | "warning" | "info"
    level: Mapped[str] = mapped_column(String(20),  nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(100), nullable=False)

    message: Mapped[str]           = mapped_column(Text,       nullable=False)
    sku:     Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Use the timestamp set by the agent, not server_default
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# restock_recommendations  — pre-wired for Restock Agent (not yet built)
# ══════════════════════════════════════════════════════════════════════════════

class RestockRecommendationRecord(Base):
    """
    Pending restock orders produced by the Restock Agent.
    Status starts as "pending_approval". Dashboard shows these for human review.
    No auto-ordering — always requires explicit human approval.
    """
    __tablename__ = "restock_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id:   Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    sku: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # ── Order details ─────────────────────────────────────────────────────────
    recommended_quantity:    Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    urgency:                 Mapped[str]   = mapped_column(String(20), nullable=False)
    days_of_stock_remaining: Mapped[float] = mapped_column(Float, nullable=False)
    units_per_day:           Mapped[float] = mapped_column(Float, nullable=False)

    reason:           Mapped[str] = mapped_column(Text, nullable=False, default="")
    supplier_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # "pending_approval" | "approved" | "ordered" | "cancelled"
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending_approval")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )