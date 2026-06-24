from typing import Optional
from pydantic import BaseModel, Field


class RestockDecisionOut(BaseModel):
    """One restock decision per candidate SKU."""
    sku:           str
    product_title: str
    variant_title: str

    should_restock: bool
    skip_reason:    Optional[str] = Field(
        default=None,
        description="Why skipped. Only set when should_restock=False.",
    )

    # Quantity
    recommended_quantity:    int   = Field(ge=0)
    urgency:                 str   = Field(description='"critical" | "high"')
    days_of_stock_remaining: float
    units_per_day:           float
    current_stock:           int

    # Supplier
    supplier_type: str = Field(
        description=(
            '"lahore_local"   → 10d lead. Pakistani fabric items: kurtas, suits, lawn, co-ords. '
            '"karachi_trader" → 7d lead. Basics + staples. '
            '"china_import"   → 32d lead incl. customs. Accessories + novelty only. '
            'NEVER china_import for urgency=critical.'
        )
    )
    estimated_lead_days:    int
    expected_stockout_date: str = Field(description="ISO date YYYY-MM-DD when stock hits 0.")
    order_deadline:         str = Field(
        description=(
            "ISO date YYYY-MM-DD — LATEST date the order can be placed without a stockout gap. "
            "Formula: expected_stockout_date − estimated_lead_days. "
            "If order_deadline < today → already late, flag as overdue."
        )
    )
    is_overdue: bool = Field(
        default=False,
        description="True if order_deadline < today. Stockout gap is unavoidable without expedited sourcing.",
    )

    # Financials (heuristic estimates — no COGS in Shopify)
    estimated_unit_cost_pkr:  Optional[float] = Field(
        default=None,
        description=(
            "Estimated landed cost per unit in PKR based on product category. "
            "Use heuristics: lawn/cotton=900, khaddar=1400, chiffon/formal=2200, "
            "co-ord=1800, cargo/bottoms=900, accessories=500. "
            "None if category unclear."
        ),
    )
    estimated_total_cost_pkr: Optional[float] = Field(
        default=None,
        description="estimated_unit_cost_pkr × recommended_quantity. None if unit cost unknown.",
    )

    reason:           str
    supplier_message: str = Field(
        description=(
            "Individual SKU-level WhatsApp message in Urdu-English mix. "
            "Used as input when building the consolidated supplier batch message. "
            "Keep under 150 words."
        )
    )
    priority: int = Field(
        description=(
            "1 = highest. Sort order: overdue first, then critical by stockout date, "
            "then high by stockout date."
        )
    )
    status: str = Field(
        default="pending_approval",
        description="Always 'pending_approval' — no auto-ordering. Humans approve every PO.",
    )


class SupplierBatch(BaseModel):
    """
    All restock SKUs from the same supplier consolidated into one WhatsApp message.
    Real Pakistani supplier relationships work in one chat thread — not per-SKU messages.
    """
    supplier_type:      str
    estimated_lead_days: int
    skus:               list[str]
    total_units:        int
    estimated_batch_cost_pkr: Optional[float] = Field(
        default=None,
        description="Sum of estimated_total_cost_pkr across all SKUs in this batch.",
    )
    consolidated_message: str = Field(
        description=(
            "One complete WhatsApp message covering ALL SKUs for this supplier. "
            "Urdu-English mix. Lists each SKU with quantity and delivery requirement. "
            "More natural than sending 3 separate messages to the same supplier. "
            "Keep under 300 words."
        )
    )


class RestockAnalysis(BaseModel):
    """Complete structured output from the Restock subagent."""
    decisions:       list[RestockDecisionOut]
    supplier_batches: list[SupplierBatch] = Field(
        description=(
            "One batch per unique supplier_type with ≥1 should_restock=True decision. "
            "This is what the founder actually sends — one message per supplier, not per SKU."
        )
    )

    total_units_to_order:      int
    estimated_total_spend_pkr: Optional[float] = Field(
        default=None,
        description="Sum of all estimated_total_cost_pkr values. None if any cost is unknown.",
    )
    critical_count: int
    high_count:     int
    overdue_count:  int
    skipped_count:  int

    summary: str = Field(
        description=(
            "2-3 sentences. Lead with overdue or critical orders. "
            "Mention total units, supplier count, estimated spend. "
            "Example: '2 overdue orders (stockout gap unavoidable): FOS-001-S, FOS-003-M. "
            "Total 180 units across 2 local suppliers (~PKR 162,000). "
            "1 SKU skipped — Pricing Agent has it on clearance.'"
        )
    )