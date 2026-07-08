from typing import Optional
from pydantic import BaseModel, Field


class SnapshotOut(BaseModel):
    """
    One row per variant SKU. As of this rewrite, this is populated directly by
    Python (agents/inventory/graph.py::compute_snapshots) — NOT by the LLM.
    Kept as a Pydantic model anyway so we get free validation (ge=0 etc.) on
    numbers computed deterministically, and so field names line up 1:1 with
    agents.state.InventorySnapshot for a trivial dict -> TypedDict pass-through.
    """
    sku:           str
    product_title: str
    variant_title: str
    current_stock: int = Field(ge=0)

    # Legacy-compatible fields — other agents (Pricing, Restock, Content, DM,
    # Marketing) read these two directly. Their MEANING got smarter:
    # units_per_day is now the 7-day velocity (more responsive than a flat
    # 14-day average), and days_of_stock_remaining is now seasonally adjusted.
    units_per_day:           float = Field(ge=0.0)
    days_of_stock_remaining: float
    urgency:                 str = Field(description='"critical" (<7d) | "high" (7-14d) | "normal" (14-30d) | "healthy" (>30d)')

    # Velocity diagnostics
    velocity_7d:         float = Field(ge=0.0)
    velocity_30d:        float = Field(ge=0.0)
    velocity_trend:      str   = Field(description='"accelerating" | "stable" | "decelerating" | "new_item" | "no_movement"')
    velocity_confidence: str   = Field(description='"high" (10+ units/30d) | "medium" (3-10) | "low" (<3)')

    # Seasonal awareness
    seasonal_multiplier_applied:        float
    seasonal_context:                   str
    days_of_stock_remaining_unadjusted: float = Field(description="Naive current_stock / velocity_7d, no seasonal adjustment — for comparison.")

    # Actionability
    reorder_point_units:  int  = Field(ge=0, description="Stock level at which a restock should already be placed (default 10d lead + 7d buffer, seasonally scaled).")
    has_pending_restock:  bool = Field(default=False, description="True if a restock_recommendation already exists in pending_approval/approved/ordered status.")
    pending_restock_note: Optional[str] = Field(default=None)

    # Size-curve anomaly (computed across sibling variants of the same product)
    size_curve_deviation: bool = Field(default=False, description="True when L/XL variants are outselling S/M for this product.")
    size_curve_note:      Optional[str] = Field(default=None)


class AlertOut(BaseModel):
    level:   str = Field(description='One of: "critical", "warning", "info"')
    message: str = Field(description="Human-readable alert. Be specific — include SKU, numbers, urgency.")
    sku:     Optional[str] = Field(default=None, description="SKU this alert relates to, if applicable.")


class InventoryAlertsAndSummary(BaseModel):
    """
    What the LLM actually produces now. All snapshot numbers — velocity, trend,
    seasonal adjustment, reorder point, urgency — are computed deterministically
    in Python before this call. The model's job is judgment: decide which
    snapshots deserve an alert and write it well, then summarize.
    """
    alerts: list[AlertOut]
    summary: str = Field(
        description=(
            "2-3 sentences. Lead with the most urgent thing — call out seasonal "
            "ramp-ups by name if they're driving urgency (e.g. 'Eid ul Fitr in "
            "21 days — 3 SKUs will miss their reorder window at current pace'). "
            "Mention dead stock and sizing anomalies. If nothing needs action, say so."
        )
    )