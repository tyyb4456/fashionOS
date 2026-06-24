from typing import Optional
from pydantic import BaseModel, Field


class InstagramOut(BaseModel):
    """Full Instagram feed post plan."""
    caption: str = Field(
        description=(
            "Full caption: hook (1 line, never starts with brand/product name) → "
            "product body (fabric, cut, occasion — 1-2 lines) → CTA (ONE action). "
            "Urdu-English mix natural. 80-150 words. "
            "NEVER: stunning, gorgeous, must-have, elevate your look, look no further."
        )
    )
    hashtags: list[str] = Field(
        description=(
            "20-25 hashtags WITHOUT the # symbol. Mix: "
            "5 broad PK (PakistaniFashion, OOTDPakistan, ...) + "
            "5 product-specific + 5 occasion/style + 3-5 niche + "
            "2-3 pulled from active trend signal keywords (if available)."
        )
    )
    story_hook: Optional[str] = Field(
        default=None,
        description=(
            "Instagram story slide hook — 1-2 sentences. DIFFERENT from caption hook. "
            "More exclusive/urgent tone. Use 'Swipe up' or 'Link in bio' as implied CTA. "
            "Example: 'The olive set everyone is asking about — finally restocked. Kal khatam ho jayega.'"
        )
    )
    optimal_post_time: str = Field(description="Always '20:00 PKT' for feed posts.")


class TikTokOut(BaseModel):
    """Full TikTok video script — 4 timed sections."""
    hook: str = Field(
        description=(
            "0-3 seconds. SHOW the outcome first — outfit on, result visible. "
            "POV format or direct result. Max 2 short sentences. "
            "Good: 'POV: You finally found your Eid outfit' or "
            "'Cargo pants ka season aa gaya Pakistan mein.'"
        )
    )
    context: str = Field(
        description=(
            "3-8 seconds. Relatable problem or occasion setup. 1-2 sentences. "
            "Urdu-English natural. 'Koi acha outfit nahi milta summer mein...' "
            "or 'Wedding season start ho gaya and you have nothing new.'"
        )
    )
    reveal: str = Field(
        description=(
            "8-20 seconds. Product details: fabric, fit, sizes available, price in PKR. "
            "If on sale: state original and new price. 3-5 sentences. "
            "Mention at least one specific: fabric composition, cut name, or occasion suitability."
        )
    )
    cta: str = Field(
        description=(
            "Last 3 seconds. ONE clear action. "
            "'DM us SIZE for size guide' / 'Link in bio — sirf X pieces bache hain.' "
            "Max 2 short sentences."
        )
    )
    optimal_post_time: str = Field(description="Always '19:00 PKT' for TikTok.")


class ShotListItem(BaseModel):
    """One specific shot the creator must film or photograph."""
    shot_number: int = Field(description="1-indexed. Lower = higher priority.")
    description: str = Field(
        description=(
            "Specific, actionable. Include: what to film, angle, background, lighting, "
            "styling, props. No vague adjectives. "
            "Good: 'Flat lay on white bedsheet, natural window light from left, "
            "fold garment to show texture, no accessories in frame.' "
            "Bad: 'Beautiful close-up shot.'"
        )
    )
    platform: str = Field(description='"instagram" | "tiktok" | "both"')
    shot_type: str = Field(
        description='"flat_lay" | "mirror_try_on" | "detail_closeup" | "lifestyle" | "measurement" | "pricing_card" | "transition"'
    )


class ContentPostOut(BaseModel):
    """Complete content plan for one product — all platforms, full creator brief."""

    sku:           str
    product_title: str
    variant_title: str

    # ── Urgency & Strategy ────────────────────────────────────────────────────
    is_urgent: bool = Field(
        description="True = post TODAY. False = schedule this week."
    )
    urgency_reason: str = Field(
        description=(
            "Why urgent or not — include specific data. "
            "Good: 'Trending on TikTok PK (score=0.87, rising) — post today to catch the wave before it peaks.' "
            "or 'On 15% markdown — schedule this week to drive clearance velocity.'"
        )
    )
    content_angle: str = Field(
        description=(
            "Strategic content angle. Exactly one of: "
            '"trending"         — riding active social trend signal | '
            '"markdown_push"    — drive sales on discounted stock | '
            '"clearance_push"   — deep discount, scarcity + FOMO | '
            '"ad_content_sync"  — active Meta campaign, organic amplifies paid | '
            '"lifestyle"        — brand building, no urgency signal'
        )
    )
    post_date_suggestion: str = Field(
        description='"today" | "tomorrow" | "within-3-days" | "this-week"'
    )

    # ── Platform Content ──────────────────────────────────────────────────────
    instagram: InstagramOut
    tiktok:    TikTokOut

    # ── Creator Guidance ──────────────────────────────────────────────────────
    creator_notes: str = Field(
        description=(
            "Overall filming/photography direction for this SKU. Setting, mood, lighting, "
            "styling, what to wear alongside. 2-3 sentences — actionable over aesthetic. "
            "Mandatory additions when has_return_issue=True — see return handling rules."
        )
    )
    shot_list: list[ShotListItem] = Field(
        description=(
            "3-5 specific shots in priority order. Must cover: flat lay (always #1), "
            "mirror/try-on (#2, primary TikTok hook source), detail closeup (#3), "
            "lifestyle context (#4). Add measurement or pricing card shot conditionally."
        )
    )

    # ── Context Flags ─────────────────────────────────────────────────────────
    is_trending:       bool
    trend_keyword:     Optional[str]  = None
    trend_platform:    Optional[str]  = None
    trend_score:       Optional[float] = None
    trend_direction:   Optional[str]  = None

    is_on_sale:        bool
    discount_pct:      float = Field(default=0.0)
    sale_mention:      Optional[str]  = Field(
        default=None,
        description="'Now PKR X,XXX (was PKR X,XXX)' — only when is_on_sale=True."
    )

    has_return_issue:  bool = Field(default=False)
    return_issue_type: Optional[str] = Field(
        default=None,
        description='"size_issue" | "color_mismatch" | "quality_issue" | "description_mismatch"'
    )

    has_active_campaign: bool = Field(
        default=False,
        description="True if marketing-agent shows a pending increase_budget or held active campaign for this SKU."
    )

    current_stock: int = Field(description="For 'limited stock' copy decisions.")
    status: str = Field(default="pending")


class ContentFatigueSkip(BaseModel):
    """A SKU that was eligible but excluded — documented for supervisor transparency."""
    sku:           str
    product_title: str
    skip_reason:   str = Field(
        description=(
            "One of: "
            '"oos"                          — stock ≤ 5, no point driving traffic | '
            '"trend_clearance_contradiction" — trending AND on clearance, contradictory | '
            '"critical_stock_no_restock"    — < 7 days stock, not trending, too risky | '
            '"cap_reached"                  — 5 candidate cap hit, lower priority than others | '
            '"clearance_low_qty"            — clearance but stock ≤ 20, not worth promoting'
        )
    )


class ContentPlan(BaseModel):
    """Complete structured output from the Content subagent."""
    posts: list[ContentPostOut] = Field(
        description="Sorted: today-posts first by trend_score desc, then this-week posts."
    )
    fatigue_skips: list[ContentFatigueSkip] = Field(
        description="All eligible SKUs that were excluded and why. Helps supervisor explain gaps to founder."
    )
    priority_today_skus: list[str] = Field(
        description="SKU list where post_date_suggestion='today'. Quick reference for urgency dashboard."
    )
    total_posts:   int
    urgent_count:  int = Field(description="Count where is_urgent=True.")
    summary: str = Field(
        description=(
            "2-3 sentences. Lead with today's urgent posts (SKUs + angles). "
            "Mention total scheduled posts and any skips with their reasons. "
            "Example: '2 urgent posts today: FOS-001-S (cargo trending TikTok PK, score=0.87) "
            "and FOS-005-M (15% markdown push). 1 lifestyle post scheduled this week. "
            "FOS-003-S skipped — OOS (3 units left).'"
        )
    )