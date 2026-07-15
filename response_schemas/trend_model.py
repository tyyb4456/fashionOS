from typing import Optional
from pydantic import BaseModel, Field


class TrendSignalOut(BaseModel):
    """
    One trend signal — one keyword × one platform. This is the ONLY thing the
    ReAct agent (Node 2 — run_react_agent) judges: score, direction, SKU
    match, and a plain-language evidence trail. Alert eligibility and the
    new-product-opportunity flag are both fully derivable from these fields
    and are computed deterministically afterward (Node 3 —
    compute_trend_alerts), not self-reported here.
    """
    keyword:   str
    platform:  str   = Field(description='"tiktok" | "instagram" | "google_trends"')
    score:     float = Field(ge=0.0, le=1.0, description="Relative trend strength 0.0–1.0.")
    direction: str   = Field(description='"rising" | "peaking" | "declining"')
    matched_sku: Optional[str] = Field(
        default=None,
        description=(
            "SKU of the closest matching product in the catalog. "
            "Match on product_title + variant_title + tags. "
            "None if confidence < 50%."
        ),
    )
    evidence: str = Field(
        description=(
            "1-2 sentences: platform searched, engagement numbers seen, SKU match rationale, "
            "and how this compares to the last recorded reading for this keyword if one was given. "
            "E.g. 'Cargo pants 200k+ views on #PakistaniFashion TikTok — "
            "FOS-001-S (Olive Cargo Pants, Small) matched on title + tag. "
            "Score up from 0.58 two days ago — accelerating.'"
        )
    )


class TrendFindings(BaseModel):
    """
    The ONLY structured output of the ReAct loop (Node 2 — run_react_agent).
    Alert computation (critical/info thresholds, history-aware duplicate
    suppression) and the new-product-opportunity flag happen downstream in
    Python (Node 3 — compute_trend_alerts) — both are fixed formulas
    derivable from score/direction/matched_sku, not judgment calls.
    """
    trend_signals: list[TrendSignalOut] = Field(
        description="All strong signals found (score >= 0.3). Sorted by score descending."
    )
    summary: str = Field(
        description=(
            "2-3 sentences. Lead with the strongest signal and its score. "
            "Mention catalog matches and any signals that moved significantly "
            "since their last recorded reading, if history was given."
        )
    )