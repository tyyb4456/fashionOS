from typing import Annotated, Optional
import operator
from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from agents.state import AgentAlert, InventorySnapshot, PricingRecommendation, TrendSignal

# ── Pydantic output schema ─────────────────────────────────────────────────────

class ContentPost(BaseModel):
    """Full content plan for one product — Instagram + TikTok."""

    sku:           str
    product_title: str
    variant_title: str

    is_urgent: bool = Field(
        description=(
            "True = trending product — post TODAY. "
            "False = on-sale or regular — schedule for this week."
        )
    )

    # ── Instagram ──────────────────────────────────────────────────────────────
    instagram_caption: str = Field(
        description=(
            "Full Instagram caption following the formula:\n"
            "1. Hook (1 line) — trend reference, relatable feeling, or bold claim. "
            "Never start with the brand name or product name.\n"
            "2. Product description woven naturally (1-2 lines) — fabric, cut, occasion.\n"
            "3. CTA (1 line) — DM 'WANT IT', link in bio, or 'limited stock' ONLY if "
            "current_stock < 20 units.\n"
            "Tone: conversational, Urdu-English mix is natural. "
            "NEVER use: stunning, gorgeous, look no further, must-have.\n"
            "Target: 80-150 words."
        )
    )
    instagram_hashtags: list[str] = Field(
        description=(
            "20-25 hashtags WITHOUT the # symbol. Mix:\n"
            "- 5 broad PK: PakistaniFashion, PakistaniOutfits, FashionTikTokPK, "
            "OutfitOfTheDay, OOTD\n"
            "- 5 product-specific: e.g. CargoPants, CoOrdSet, LawnSuit, KurtaKameez\n"
            "- 5 occasion/style: e.g. EidOutfit, SummerFashion, CasualWear, ModestFashion\n"
            "- 3-5 niche: e.g. PakistaniFashionBlogger, DesiStyle, KarachiStyle, LahoreStyle\n"
            "- 2-3 trending: match the trend keyword if available"
        )
    )

    # ── TikTok ─────────────────────────────────────────────────────────────────
    tiktok_hook: str = Field(
        description=(
            "0-3 seconds. Start WITH the end result — show the outfit immediately. "
            "Must grab attention in the first frame before anyone scrolls past.\n"
            "Good: 'POV: You finally found your Eid outfit' / "
            "'This is why cargo pants are going viral in Pakistan'\n"
            "Max 2 short sentences."
        )
    )
    tiktok_context: str = Field(
        description=(
            "3-8 seconds. Occasion setup or relatable problem.\n"
            "Examples: 'Koi acha outfit hi nahi milta summer mein...' / "
            "'Wedding season aa gaya and you have nothing to wear?'\n"
            "1-2 sentences."
        )
    )
    tiktok_reveal: str = Field(
        description=(
            "8-20 seconds. Product details, styling tips, price, where to buy.\n"
            "Mention: fabric name, fit style, available sizes, price in PKR.\n"
            "If on sale: state original price and new price.\n"
            "3-5 sentences."
        )
    )
    tiktok_cta: str = Field(
        description=(
            "Last 3 seconds. ONE clear action.\n"
            "Options: 'DM us SIZE to get the size guide' / "
            "'Link in bio for easy ordering' / "
            "'Sirf X pieces bache hain — abhi order karo!'\n"
            "Max 2 short sentences."
        )
    )

    # ── Scheduling ─────────────────────────────────────────────────────────────
    optimal_post_time_instagram: str = Field(
        description="Best Instagram post time. Use '20:00 PKT' (8 PM Pakistan Standard Time)."
    )
    optimal_post_time_tiktok: str = Field(
        description="Best TikTok post time. Use '19:00 PKT' (7 PM Pakistan Standard Time)."
    )

    # ── Creator guidance ────────────────────────────────────────────────────────
    creator_notes: str = Field(
        description=(
            "Specific filming/photography instructions. Include: "
            "setting/background, lighting, angles, styling, props, mood.\n"
            "2-3 actionable sentences.\n"
            "Example: 'Film in natural daylight near a window. "
            "Do a flat lay showing the full outfit, then a mirror try-on. "
            "Style with white sneakers — keep accessories minimal so the pants are the hero.'"
        )
    )

    # ── Sale context ────────────────────────────────────────────────────────────
    sale_mention: Optional[str] = Field(
        default=None,
        description=(
            "If product is on markdown, the exact sale text to include in captions.\n"
            "Format: 'Now PKR X,XXX (was PKR X,XXX)'\n"
            "None if product is at full price."
        )
    )


class ContentPlan(BaseModel):
    posts:   list[ContentPost]
    summary: str = Field(
        description=(
            "2-3 sentences on this content batch. "
            "How many posts, which are urgent, key themes to film first.\n"
            "Example: '4 posts ready: 2 urgent (trending cargo pants + co-ord set — film today), "
            "2 scheduled for this week (markdown promos). "
            "Prioritise the TikTok hook for cargo pants — highest trend score.'"
        )
    )