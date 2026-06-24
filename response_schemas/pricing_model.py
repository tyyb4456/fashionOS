from typing import Optional
from pydantic import BaseModel, Field


class PricingDecisionOut(BaseModel):
    """One pricing decision per variant SKU."""
    sku:           str
    variant_id:    int
    product_title: str
    variant_title: str

    current_price:        float = Field(ge=0)
    compare_at_price:     float = Field(ge=0, description="0 if not currently on markdown.")
    recommended_price:    float = Field(ge=0)
    new_compare_at_price: Optional[float] = Field(
        default=None,
        description=(
            "Value to set as compare_at_price (the 'was' price strikethrough). "
            "First markdown: set to current_price (original). "
            "Subsequent rungs: keep original compare_at_price — never reset it. "
            "None for hold."
        ),
    )

    action: str = Field(
        description=(
            '"hold"           = no price change. '
            '"markdown"       = reduce price, set compare_at_price. '
            '"increase"       = raise price (trending or premium positioning). '
            '"clearance_code" = deepest markdown + create a discount code. '
            '"bundle"         = flag for manual bundle creation (human required).'
        )
    )
    discount_pct:  float = Field(ge=0, le=100, description="0 for hold and increase.")
    markdown_rung: int   = Field(
        default=0,
        description="Rung AFTER this action. 0=full price, 1≈15% off, 2≈25% off, 3=clearance ≥35%.",
    )

    auto_execute: bool = Field(
        description="True = execute immediately via Shopify API in this run."
    )
    executed: bool = Field(
        default=False,
        description="Set to True after successful update_product_price call.",
    )
    execution_result: Optional[str] = Field(
        default=None,
        description="'success' | error message string. Populated after execution attempt.",
    )

    suggested_discount_code: Optional[str] = Field(
        default=None,
        description="For clearance_code action only. Format: CLEAR-{SKU_SLUG}-{YYYYMM}.",
    )
    reason: str = Field(
        description=(
            "1-2 sentences. Include: trigger, velocity/days numbers, margin context. "
            "Example: 'FOS-001 zero velocity for 52 days (dead stock). "
            "First markdown rung: PKR 2999 → PKR 2549 (15% off).'"
        )
    )


class PricingAnalysis(BaseModel):
    """Complete structured output the Pricing subagent produces each run."""
    decisions:           list[PricingDecisionOut]
    auto_executed_count: int   = Field(description="Actions executed in this run.")
    pending_count:       int   = Field(description="Actions queued for human approval.")
    failed_count:        int   = Field(default=0, description="Execution attempts that errored.")
    summary: str = Field(
        description=(
            "2-3 sentences. Lead with what was auto-executed. "
            "Mention pending approvals with most urgent SKU. "
            "Example: '3 first-rung markdowns auto-executed (15% off). "
            "1 price increase auto-applied on trending FOS-019. "
            "2 clearance candidates queued for approval (>45 days dead stock).'"
        )
    )