from typing import Optional
from pydantic import BaseModel, Field



# ── Pydantic output schema ─────────────────────────────────────────────────────

class ReturnPattern(BaseModel):
    """Analysis result for one SKU with returns."""

    sku:           str
    product_title: str

    total_returns:        int   = Field(ge=0)
    total_units_returned: int   = Field(ge=0)

    primary_reason: str = Field(
        description=(
            "The dominant return reason category. Exactly one of:\n"
            "size_issue | description_mismatch | quality_issue | "
            "changed_mind | late_delivery | duplicate_order | other"
        )
    )
    reason_breakdown: dict = Field(
        description=(
            "Count per reason category for this SKU. "
            "e.g. {'size_issue': 4, 'description_mismatch': 1, 'changed_mind': 1}"
        )
    )
    evidence: str = Field(
        description=(
            "Paraphrase of the actual customer reason text. Do NOT quote verbatim — "
            "summarise the pattern in 1 sentence. "
            "Example: 'Most customers said the kurta runs small and the size chart was misleading.'"
        )
    )

    return_rate_pct: Optional[float] = Field(
        default=None,
        description=(
            "Return rate % = (total_units_returned / estimated_30d_sales) × 100. "
            "None if sales data unavailable."
        )
    )
    estimated_30d_sales: Optional[int] = Field(
        default=None,
        description="units_per_day × 30, from Inventory Agent data. None if unavailable."
    )

    severity: str = Field(
        description=(
            "Based on return_rate_pct if available, else absolute counts. "
            "critical: rate > 15% or > 10 units | "
            "warning: rate 10-15% or 6-10 units | "
            "info: rate 5-10% or 3-5 units | "
            "healthy: rate < 5% or < 3 units (skip — don't generate alerts for healthy)"
        )
    )

    recommended_fix: str = Field(
        description=(
            "Specific, actionable 1-2 sentence recommendation based on primary_reason.\n"
            "Must reference the actual product and reason. Not generic.\n"
            "Examples:\n"
            "size_issue → 'Add a size guide table with chest/waist/hip in cm and inches "
            "to the product page. Note whether this style runs true to size or slim fit.'\n"
            "quality_issue → 'Flag this batch to the supplier immediately and request "
            "a quality hold. Do not restock until the stitching issue is resolved.'\n"
            "description_mismatch → 'Reshoot in natural outdoor light and add "
            "a color accuracy note. Include exact fabric weight (gsm) in the description.'"
        )
    )

    fix_type: str = Field(
        description=(
            "Category for the dashboard fix queue. One of:\n"
            "update_size_guide | update_photos | update_description | "
            "quality_review | contact_supplier | monitor | no_action"
        )
    )


class ReturnsAnalysis(BaseModel):
    patterns:               list[ReturnPattern]
    total_returns_analyzed: int
    skus_analyzed:          int
    summary: str = Field(
        description=(
            "2-3 sentence operational summary.\n"
            "Example: '18 returns analyzed across 4 SKUs in the last 30 days. "
            "FOS-002 has a 22% return rate — size guide update is critical. "
            "FOS-001 returns are all changed_mind post-Eid — no product fix needed.'"
        )
    )



