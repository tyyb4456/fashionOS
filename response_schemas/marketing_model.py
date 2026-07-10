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

# ══════════════════════════════════════════════════════════════════════════════
# Deterministic-math rewrite additions
# Node 2 (compute_marketing_plan) computes CampaignPlanItem entirely in Python —
# the decision framework (SKU match → budget control → stock → clearance →
# trend → organic viral → ROAS → healthy) is a rule table, not judgment.
# Node 3 (generate_marketing_copy) is the ONLY LLM call — it only writes
# `reason` text for non-hold campaigns and a summary. CampaignDecision /
# MarketingAnalysis above are kept for backward compat.
# ══════════════════════════════════════════════════════════════════════════════

class CampaignPlanItem(BaseModel):
    """Deterministically computed by agents/marketing/graph.py::compute_marketing_plan. No LLM involved."""
    campaign_id:   str
    campaign_name: str
    sku:           Optional[str] = None
    follows_convention: bool

    current_status:     str
    has_daily_budget:    bool
    current_budget_pkr:   float

    roas_7d:        Optional[float] = None
    spend_7d_pkr:    float = 0.0
    ctr_7d:           float = 0.0
    no_spend_data:     bool = True

    action:         str     # "hold" | "increase_budget" | "decrease_budget" | "pause" | "activate"
    new_budget_pkr:  Optional[float] = None
    change_pct:       float = 0.0
    auto_execute:      bool
    trigger:            str  # "no_sku_match" | "no_budget_control" | "out_of_stock" | "clearance" |
                              # "trending_increase" | "trending_hold_low_roas" | "organic_viral" |
                              # "low_roas_pause" | "low_roas_decrease" | "healthy"


class MarketingCopyOut(BaseModel):
    """LLM-authored reason for one non-hold campaign. Every number is already final —
    reference it, never recompute or contradict it."""
    campaign_id: str
    reason:       str = Field(description="1-2 sentences referencing the given action, numbers, and trigger context.")


class MarketingCopyPlan(BaseModel):
    """The ONLY structured LLM output for the Marketing Agent."""
    items:   list[MarketingCopyOut]
    summary: str = Field(
        description=(
            "2-3 sentence operational summary. Lead with what's paused/auto-executed. "
            "Mention pending budget increases with the most promising SKU."
        )
    )