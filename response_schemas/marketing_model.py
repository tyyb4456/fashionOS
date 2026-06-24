from typing import Optional
from pydantic import BaseModel, Field


class CampaignDecisionOut(BaseModel):
    """One budget/status decision per Meta campaign."""

    # Campaign identity
    campaign_id:   str
    campaign_name: str
    matched_sku:   Optional[str] = Field(
        default=None,
        description=(
            "SKU extracted from campaign name via FashionOS_{SKU}_{desc} convention. "
            "None if name doesn't follow the convention — conservative hold applied."
        ),
    )

    # Current campaign state (from get_campaigns)
    current_daily_budget_pkr: float = Field(ge=0)
    current_status:           str   # "ACTIVE" | "PAUSED"
    has_daily_budget:         bool  # False = ad-set level budgets, can't change here

    # 7-day performance context (from get_campaign_performance)
    roas_7d:       Optional[float] = Field(
        default=None,
        description="Purchase ROAS. None if Meta Pixel not configured or no conversions.",
    )
    spend_7d_pkr:  float = Field(default=0.0, ge=0)
    ctr_7d:        float = Field(default=0.0, ge=0)
    no_spend_data: bool  = Field(
        default=False,
        description="True if campaign had zero spend in the 7-day window.",
    )

    # Decision output
    action: str = Field(
        description=(
            "'hold'            = no change. "
            "'pause'           = stop spending immediately. "
            "'activate'        = resume paused campaign (always pending_approval). "
            "'increase_budget' = raise daily budget (always pending_approval). "
            "'decrease_budget' = reduce daily budget (auto if |change_pct| ≤ 30)."
        )
    )
    new_daily_budget_pkr: Optional[float] = Field(
        default=None,
        description=(
            "Target daily budget in PKR. Set for increase/decrease. "
            "Rounded to nearest PKR 50. None for hold/pause/activate. "
            "Minimum PKR 200 — if decrease would go below, use 'pause' instead."
        ),
    )
    change_pct: float = Field(
        default=0.0,
        description=(
            "% change from current_daily_budget_pkr. "
            "Positive = increase, negative = decrease, 0 for hold/pause/activate."
        ),
    )
    trigger: str = Field(
        description=(
            "What drove this decision: "
            "'out_of_stock' | 'clearance' | 'trending_good_roas' | 'trending_no_roas' "
            "| 'trending_reactivate' | 'organic_viral' | 'very_low_roas' | 'low_roas' "
            "| 'paused_trending' | 'healthy' | 'no_sku_match' | 'no_budget_control'"
        )
    )
    auto_execute: bool = Field(
        description=(
            "True = execute via Meta API in this run. "
            "False = queue for human approval. "
            "Hard rule: auto=True ONLY for hold, pause, decrease_budget ≤ 30%. "
            "auto=False ALWAYS for increase_budget and activate."
        )
    )
    reason: str = Field(
        description=(
            "1-2 sentences with real numbers. "
            "e.g. 'FOS-001-S out of stock (3 units) — pausing to stop driving traffic.' "
            "OR 'Cargo pants trend score 0.82 rising on TikTok PK, ROAS 3.1 — "
            "PKR 500 → PKR 650 increase queued for approval.'"
        )
    )

    # Execution tracking (same pattern as PricingDecisionOut)
    executed: bool = Field(
        default=False,
        description="Set to True after a successful Meta API call.",
    )
    execution_result: Optional[str] = Field(
        default=None,
        description="'success' | error string. Populated after each execution attempt.",
    )


class MarketingAnalysis(BaseModel):
    """Complete structured output for one Marketing Agent run."""

    decisions:          list[CampaignDecisionOut]

    auto_executed_count: int = Field(
        description="Actions executed via Meta API this run (executed=True, action != hold)."
    )
    pending_count: int = Field(
        description="Actions queued for human approval (auto_execute=False, action != hold)."
    )
    failed_count: int = Field(
        default=0,
        description="Execution attempts where the Meta API call errored (auto_execute=True, executed=False).",
    )
    paused_count: int = Field(
        default=0,
        description="Campaigns successfully paused this run.",
    )

    summary: str = Field(
        description=(
            "2-3 sentences. Lead with what was auto-executed. Mention pending approvals. "
            "Example: '8 campaigns analysed. 2 auto-paused (FOS-002 out of stock, FOS-007 clearance). "
            "1 budget increase queued for approval (+25% on FOS-001, trending TikTok PK, ROAS 3.1). "
            "5 held — performance within normal range.'"
        )
    )