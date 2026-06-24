from typing import Optional
from pydantic import BaseModel, Field


class SnapshotOut(BaseModel):
    """One row per variant SKU."""
    sku:                     str
    product_title:           str
    variant_title:           str
    current_stock:           int   = Field(ge=0)
    units_per_day:           float = Field(ge=0.0)
    days_of_stock_remaining: float = Field(
        description=(
            "current_stock / units_per_day. "
            "Set to 999.0 for zero-velocity SKUs (no sales in window)."
        )
    )
    urgency: str = Field(
        description=(
            'Exactly one of: "critical" (<7 days), "high" (7–14 days), '
            '"normal" (14–30 days or zero-velocity), "healthy" (>30 days).'
        )
    )


class AlertOut(BaseModel):
    level:   str = Field(description='One of: "critical", "warning", "info"')
    message: str = Field(description="Human-readable alert. Be specific — include SKU, numbers, urgency.")
    sku:     Optional[str] = Field(default=None, description="SKU this alert relates to, if applicable.")


class InventoryAnalysis(BaseModel):
    """Complete structured output the Inventory Agent produces."""
    inventory_snapshots: list[SnapshotOut] = Field(
        description="One entry per active variant SKU. Include ALL variants."
    )
    alerts: list[AlertOut] = Field(
        description=(
            "Raise only actionable alerts. "
            "critical = stockout < 7 days. "
            "warning  = dead stock (stock > 0, zero velocity 14+ days). "
            "info     = size distribution anomaly (L/XL outselling S/M)."
        )
    )
    summary: str = Field(
        description=(
            "2–3 sentence overview of overall inventory health. "
            "Example: '14 SKUs healthy. 2 CRITICAL (restock in <7 days). "
            "3 dead stock variants flagged for markdown review.'"
        )
    )