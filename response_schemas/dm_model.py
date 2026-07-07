from typing import Optional
from pydantic import BaseModel, Field


# ── Pydantic output schema ─────────────────────────────────────────────────────

class DmDecision(BaseModel):
    """One classification + action per DM."""

    message_id:       str
    conversation_id:  str
    user_id:          str
    username:         str
    original_message: str  # Truncated to 200 chars in state

    category: str = Field(
        description=(
            "Exactly one of: "
            "size_question | availability | order_status | "
            "bulk_inquiry | complaint | influencer | spam | general_inquiry"
        )
    )

    auto_send: bool = Field(
        description=(
            "True = send reply automatically via Instagram API. "
            "True ONLY for: size_question, availability, order_status, general_inquiry. "
            "False for: bulk_inquiry, complaint, influencer, spam."
        )
    )

    reply_text: Optional[str] = Field(
        default=None,
        description=(
            "Complete reply to send. Required if auto_send=True. None if flagging. "
            "Max 500 characters. Brand voice: warm, conversational, Urdu-English mix welcome. "
            "For availability: use the inventory data provided to give accurate stock status. "
            "Never start with 'Dear Customer'. Use customer's @username if available."
        )
    )

    flag_for_human: bool = Field(
        description="True for: bulk_inquiry, complaint, influencer."
    )
    flag_priority: Optional[str] = Field(
        default=None,
        description="'high' (bulk_inquiry, complaint) | 'normal' (influencer) | None otherwise.",
    )
    flag_reason: Optional[str] = Field(
        default=None,
        description="Why this needs human attention. 1 sentence. Required if flag_for_human=True.",
    )


class DmAnalysis(BaseModel):
    decisions:        list[DmDecision]
    auto_send_count:  int
    flagged_count:    int
    summary: str = Field(
        description=(
            "2-3 sentence summary. "
            "Example: '8 DMs processed. 5 auto-replied (3 availability, 2 size questions). "
            "2 flagged: 1 bulk inquiry (50 units query from @retailer_pk) + "
            "1 complaint (damaged item). 1 spam skipped.'"
        )
    )