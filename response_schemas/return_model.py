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

# ══════════════════════════════════════════════════════════════════════════════
# Deterministic-math rewrite additions
# Node 2 (compute_return_plan) computes ReturnPlanItem entirely in Python —
# reason classification (keyword taxonomy from the fashion_returns skill),
# return-rate math, severity thresholds, fix_type mapping, and the
# recommended_fix template are all deterministic. Node 3 (generate_return_copy)
# is the ONLY LLM call — it only writes `evidence` (a paraphrase of raw
# customer text, never verbatim) and a summary. ReturnPattern/ReturnsAnalysis
# above are kept for backward compat.
# ══════════════════════════════════════════════════════════════════════════════

class ReturnPlanItem(BaseModel):
    """Deterministically computed by agents/returns/graph.py::compute_return_plan. No LLM involved."""
    sku:           str
    product_title: str

    total_returns:        int = Field(ge=0)
    total_units_returned: int = Field(ge=0)

    primary_reason:   str            # size_issue | description_mismatch | quality_issue |
                                       # changed_mind | late_delivery | duplicate_order | other
    reason_breakdown: dict[str, int]  # count per category

    return_rate_pct:     Optional[float] = None
    estimated_30d_sales: Optional[int]   = None

    severity: str   # "critical" | "warning" | "info" — "healthy" rows never make it into the plan

    fix_type:         str   # deterministic mapping from primary_reason
    recommended_fix:  str   # deterministic template, product name + count interpolated

    sample_reasons: list[str] = Field(
        default_factory=list,
        description="Up to 5 raw customer reason strings for this SKU — context for the LLM's evidence paraphrase only.",
    )


class ReasonClassificationItem(BaseModel):
    """One LLM classification for one raw customer reason string. Node 2 output — flat list, unaggregated."""
    sku:          str
    reason_index: int = Field(description="0-based index into that SKU's raw reason list — used to match back to the source text in Python.")
    category:     str = Field(description="Exactly one of: size_issue | description_mismatch | quality_issue | changed_mind | late_delivery | duplicate_order | other")


class ReasonClassificationBatch(BaseModel):
    """The ONLY structured output of Node 2 (classify_return_reasons)."""
    classifications: list[ReasonClassificationItem]


class ReturnCopyOut(BaseModel):
    """LLM-authored evidence + recommended_fix for one flagged SKU (Node 4).
    primary_reason, severity, fix_type, rate — all already final. Reference them, never contradict."""
    sku:              str
    evidence:          str = Field(description="1 sentence paraphrasing the pattern in sample_reasons. Never quote verbatim.")
    recommended_fix:   str = Field(
        description=(
            "Specific, actionable 1-2 sentence fix referencing the actual product, "
            "primary_reason, and return count. Not generic."
        )
    )


class ReturnCopyPlan(BaseModel):
    """The ONLY structured LLM output for the Returns Agent."""
    items:   list[ReturnCopyOut]
    summary: str = Field(
        description=(
            "2-3 sentence operational summary. Example: '18 returns analyzed across "
            "4 SKUs in the last 30 days. FOS-002 has a 22% return rate — size guide "
            "update is critical. FOS-001 returns are all changed_mind post-Eid — no "
            "product fix needed.'"
        )
    )