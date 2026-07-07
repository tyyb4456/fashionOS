from typing import Optional
from pydantic import BaseModel, Field

# ── Pydantic output schema ─────────────────────────────────────────────────────

class CampaignDecision(BaseModel):
    """One budget/status decision per Meta campaign."""

    campaign_id:   str
    campaign_name: str
    matched_sku:   Optional[str] = Field(
        default=None,
        description=(
            "SKU extracted from campaign name. None if name doesn't follow the "
            "FashionOS_{SKU}_{desc} convention — conservative hold applied."
        ),
    )

    # Current state
    current_daily_budget_pkr: float
    current_status:           str   # "ACTIVE" | "PAUSED"
    has_daily_budget:         bool  # False = ad-set level budgets, can't change at campaign level

    # 7-day performance context
    roas_7d:     Optional[float] = None   # None if pixel not configured
    spend_7d_pkr: float           = 0.0
    ctr_7d:       float           = 0.0
    no_spend_data: bool           = False

    action: str = Field(
        description=(
            "One of: 'hold' | 'increase_budget' | 'decrease_budget' | 'pause' | 'activate'. "
            "'hold' = no change. 'pause' = stop spending. "
            "'activate' = resume paused campaign (always pending_approval)."
        )
    )
    new_daily_budget_pkr: Optional[float] = Field(
        default=None,
        description=(
            "Target daily budget in PKR. Set for increase/decrease actions. "
            "None for hold/pause/activate. "
            "Apply budget change ceiling: ±30% max per cycle."
        ),
    )
    change_pct: float = Field(
        default=0.0,
        description=(
            "% change from current budget. Positive = increase, negative = decrease. "
            "0 for hold/pause/activate."
        ),
    )

    auto_execute: bool = Field(
        description=(
            "True = execute via Meta API now. "
            "False = queue for human approval in dashboard. "
            "Rules: "
            "auto=True for: action='hold', action='pause', "
            "action='decrease_budget' with |change_pct| ≤ 30. "
            "auto=False for: action='increase_budget', action='activate'."
        )
    )
    reason: str = Field(
        description=(
            "1-2 sentence explanation referencing actual numbers. "
            "e.g. 'FOS-001-S is out of stock (3 units) — pausing campaign to stop driving "
            "traffic to an unavailable product.' "
            "OR 'Cargo pants trend score 0.82 (rising on TikTok PK) — "
            "increasing budget from PKR 500 to PKR 650 to capture peak demand.'"
        )
    )
    trigger: str = Field(
        description=(
            "What drove this decision. One of: "
            "'out_of_stock' | 'clearance' | 'trending' | 'organic_viral' | "
            "'low_roas' | 'healthy' | 'no_sku_match' | 'no_budget_control'"
        )
    )


class MarketingAnalysis(BaseModel):
    """Complete structured output for one Marketing Agent run."""

    decisions: list[CampaignDecision]
    summary: str = Field(
        description=(
            "2-3 sentence operational summary. "
            "Example: '6 campaigns analysed. 2 paused (out-of-stock SKUs: FOS-003, FOS-007). "
            "1 budget increase queued for approval (FOS-001 trending on TikTok). "
            "3 held — performance healthy.'"
        )
    )