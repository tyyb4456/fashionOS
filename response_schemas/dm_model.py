from typing import Optional
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
# Node 2 output — classify_dms (the ONLY thing the first LLM call does)
# ══════════════════════════════════════════════════════════════════════════════

class DmClassificationOut(BaseModel):
    """One category classification for one DM. No gating, no reply text — that's Node 3/4."""
    message_id:       str
    conversation_id:  str
    user_id:          str
    username:         str
    original_message: str  # echoed back, truncated to 200 chars by the caller

    category: str = Field(
        description=(
            "Exactly one of: "
            "size_question | availability | order_status | general_inquiry | "
            "bulk_inquiry | complaint | influencer | spam. "
            "Use real understanding of the message, not keyword matching — "
            "handle Urdu-English code-switching, paraphrases, and compound questions."
        )
    )


class DmClassificationBatch(BaseModel):
    """The ONLY structured output of Node 2 (classify_dms)."""
    classifications: list[DmClassificationOut]


# ══════════════════════════════════════════════════════════════════════════════
# Node 4 output — generate_dm_replies (the ONLY other LLM call)
# category, auto_send, flag_for_human, flag_priority are already final by this
# point (Node 3's fixed lookup table) — this call ONLY writes reply text for
# auto_send=True DMs, using inventory context for availability answers.
# ══════════════════════════════════════════════════════════════════════════════

class DmReplyCopyOut(BaseModel):
    """LLM-authored reply for one auto_send=True DM."""
    message_id: str
    reply_text: str = Field(
        description=(
            "Complete reply to send. Max 500 characters. Brand voice: warm, "
            "conversational, Urdu-English mix welcome. For availability questions, "
            "use the inventory data provided to give an accurate stock status. "
            "Never start with 'Dear Customer'. Use the customer's @username if available."
        )
    )


class DmReplyCopyPlan(BaseModel):
    """The ONLY structured output of Node 4 (generate_dm_replies)."""
    items:   list[DmReplyCopyOut]
    summary: str = Field(
        description=(
            "2-3 sentence summary. Example: '8 DMs processed. 5 auto-replied "
            "(3 availability, 2 size questions). 2 flagged: 1 bulk inquiry (50 "
            "units query from @retailer_pk) + 1 complaint (damaged item). "
            "1 spam skipped.'"
        )
    )