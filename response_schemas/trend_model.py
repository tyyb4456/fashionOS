from typing import Optional
from pydantic import BaseModel, Field


class TrendSignalOut(BaseModel):
    """One trend signal — one keyword × one platform."""
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
            "1-2 sentences: platform searched, engagement numbers seen, SKU match rationale. "
            "E.g. 'Cargo pants 200k+ views on #PakistaniFashion TikTok — "
            "FOS-001-S (Olive Cargo Pants, Small) matched on title + tag.'"
        )
    )
    is_new_product_opportunity: bool = Field(
        default=False,
        description="True if score > 0.5 AND matched_sku is None.",
    )


class TrendAlertOut(BaseModel):
    level:   str            = Field(description='"critical" | "warning" | "info"')
    message: str            = Field(description="Specific: keyword, platform, engagement numbers, SKU.")
    sku:     Optional[str]  = Field(default=None)


class TrendAnalysis(BaseModel):
    """Complete structured output the Trend Agent produces."""
    trend_signals: list[TrendSignalOut] = Field(
        description="All strong signals found (score >= 0.3). Sorted by score descending."
    )
    alerts: list[TrendAlertOut] = Field(
        description=(
            "critical = score >= 0.8, rising, catalog-matched SKU exists. "
            "info     = score >= 0.5, no catalog match (new product opportunity). "
            "No alert for score < 0.5."
        )
    )
    summary: str = Field(
        description=(
            "2-3 sentences. Lead with strongest signal and its score. "
            "Mention catalog matches and new product opportunities."
        )
    )