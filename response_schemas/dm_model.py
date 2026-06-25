from typing import Optional
from pydantic import BaseModel, Field


class DmDecisionOut(BaseModel):
    """One classification + action per DM conversation."""

    message_id:       str
    conversation_id:  str
    user_id:          str
    username:         str
    original_message: str = Field(description="Truncated to 300 chars.")

    category: str = Field(
        description=(
            "Exactly one of: "
            "size_question | availability | order_status | pricing_inquiry | "
            "bulk_inquiry | complaint | influencer | spam | general_inquiry"
        )
    )

    sub_category: Optional[str] = Field(
        default=None,
        description=(
            "Optional refinement for pattern analysis. "
            "size_question   → 'fit_advice' | 'measurement_request' | 'size_comparison' | 'size_for_event'; "
            "complaint       → 'delivery_delay' | 'quality_issue' | 'wrong_item' | 'return_request' | 'color_mismatch'; "
            "bulk_inquiry    → 'reseller' | 'wedding_party' | 'corporate' | 'gifting' | 'boutique'; "
            "influencer      → 'nano_under10k' | 'micro_10k_100k' | 'macro_100k_plus' | 'unknown_tier'; "
            "pricing_inquiry → 'discount_request' | 'cod_availability' | 'bundle_price' | 'wholesale_rate'."
        )
    )

    sentiment: str = Field(
        description='"positive" | "neutral" | "frustrated" | "urgent" | "excited"'
    )

    auto_send: bool = Field(
        description=(
            "True = send reply immediately via Instagram API. "
            "True ONLY for: size_question, availability, order_status, "
            "general_inquiry, pricing_inquiry (non-discount requests). "
            "False ALWAYS for: bulk_inquiry, complaint, influencer, spam."
        )
    )

    reply_text: Optional[str] = Field(
        default=None,
        description=(
            "Complete reply. Rules by category: "
            "auto_send=True  → ready to send, ≤ 500 chars, brand voice, Urdu-English mix. "
            "complaint       → draft for human to review and edit (they may want to personalise). "
            "bulk_inquiry    → draft opener for human to confirm pricing/availability. "
            "influencer      → draft collab interest reply for human to approve. "
            "spam            → None (no reply drafted). "
            "NEVER start with 'Dear Customer'. Use @username if available. "
            "NEVER promise a specific delivery date or ad-hoc discount. "
            "OOS availability: offer restock notification, ask them to DM their size. "
            "In-stock availability: state stock exists, direct to link in bio."
        )
    )

    flag_for_human: bool = Field(
        description="True for: bulk_inquiry, complaint, influencer."
    )

    flag_priority: Optional[str] = Field(
        default=None,
        description=(
            '"critical" — frustrated complaint (churn risk) or bulk > PKR 30,000 (revenue). '
            '"high"     — any complaint, reseller bulk_inquiry, macro influencer. '
            '"normal"   — nano/micro influencer, gifting bulk, general collab inquiry. '
            'None otherwise.'
        )
    )

    flag_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why human attention is needed. 1-2 sentences. "
            "Include revenue estimate for bulk: 'Reseller requesting 50 units — ~PKR 125,000 order.' "
            "Include risk note for complaints: 'Customer received wrong color — refund or replacement needed.' "
            "Required whenever flag_for_human=True."
        )
    )

    # ── Smart fields not in graph version ──────────────────────────────────────

    products_mentioned: list[str] = Field(
        default_factory=list,
        description=(
            "Product names or SKUs explicitly mentioned in the DM. "
            "Extracted verbatim. Used for batch pattern analysis. "
            "Example: ['olive cargo pants', 'FOS-001', 'co-ord set']"
        )
    )

    estimated_order_value_pkr: Optional[float] = Field(
        default=None,
        description=(
            "For bulk_inquiry only: estimated PKR order value. "
            "Formula: mentioned_quantity × avg_product_price (use PKR 2,500 if unknown). "
            "Drives flag_priority assignment and founder briefing."
        )
    )

    follow_up_action: Optional[str] = Field(
        default=None,
        description=(
            "Recommended action beyond the reply. One of: "
            "'update_size_guide'     — high volume size questions for same product; "
            "'review_product_photos' — color_mismatch complaints; "
            "'create_return_label'   — confirmed wrong_item or quality_issue; "
            "'tag_as_vip'           — bulk reseller or repeat customer signals; "
            "'pursue_collab'        — influencer with relevant niche + decent follower count; "
            "'escalate_to_founder'  — high-value or sensitive situation; "
            "None otherwise."
        )
    )

    reply_confidence: str = Field(
        default="high",
        description=(
            '"high"   — clear category, accurate inventory data available, reply is definitive. '
            '"medium" — category clear but product not found in inventory_snapshot (guessed availability). '
            '"low"    — ambiguous message, reply is generic — human should review before it\'s auto-sent.'
        )
    )


class DmBatchSummary(BaseModel):
    """Aggregated stats and patterns across all DMs in this batch."""

    total_fetched:      int = Field(description="Total conversations fetched from Instagram.")
    total_processed:    int = Field(description="Conversations that needed a reply (needs_reply=True).")
    auto_sent_count:    int
    flagged_count:      int
    skipped_spam_count: int
    low_confidence_count: int = Field(
        description="Auto-sent replies with reply_confidence='low' — founder may want to review."
    )

    category_breakdown: dict = Field(
        description=(
            "Dict of {category: count} for all processed DMs. "
            "E.g. {'size_question': 4, 'availability': 3, 'complaint': 1, 'spam': 2}"
        )
    )

    top_products_mentioned: list[str] = Field(
        description=(
            "Product names/SKUs most frequently referenced in DMs this batch, descending. "
            "Trend signal: what customers are asking about most right now."
        )
    )

    pattern_insights: list[str] = Field(
        description=(
            "Observed patterns worth surfacing to the founder. 2-5 items. "
            "Be specific — include counts and product names. "
            "Examples: "
            "'4 size questions about Olive Cargo Pants in this batch — size guide likely needs cm measurements.', "
            "'2 color mismatch complaints for Beige Linen Dress — product photography may be misleading.', "
            "'Reseller @retailer_pk asking for 50 units — PKR 125,000 opportunity, founder should respond personally.'"
        )
    )

    action_items: list[str] = Field(
        description=(
            "Concrete, prioritised actions for the founder beyond individual DM replies. "
            "Derived from pattern_insights. "
            "Examples: "
            "'Update size guide for Olive Cargo Pants with chest/waist/length in cm (4 size questions)', "
            "'Review beige dress photos — shoot in natural daylight only (2 color mismatch reports)', "
            "'Respond personally to @retailer_pk bulk inquiry before they go to a competitor'"
        )
    )


class DmAnalysis(BaseModel):
    """Complete structured output the DM Agent produces per run."""

    decisions:   list[DmDecisionOut]
    batch_stats: DmBatchSummary

    critical_flags: list[str] = Field(
        description=(
            "Conversation IDs needing IMMEDIATE founder attention. "
            "Includes: flag_priority='critical' decisions. "
            "Shown at top of dashboard. Empty list if nothing critical."
        )
    )

    send_results: list[dict] = Field(
        default_factory=list,
        description=(
            "Execution results for auto-sent replies. "
            "Each entry: {conversation_id, username, category, sent: bool, error: str|None, sent_at: str|None}"
        )
    )

    summary: str = Field(
        description=(
            "2-3 sentences. Lead with auto-replied count and key categories. "
            "Mention flags and any pattern alerts. "
            "Example: '12 DMs processed: 8 auto-replied (4 size questions, 3 availability, 1 order status). "
            "3 flagged: 1 bulk order (~PKR 40,000 from @retailer_pk), 1 frustrated complaint. "
            "Pattern alert: high size_question volume for Cargo Pants — update size guide.'"
        )
    )