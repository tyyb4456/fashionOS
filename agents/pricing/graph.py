"""
Pricing Agent — FashionOS Phase 2 Operations
=============================================
Reads live prices, velocity, existing discounts, and inventory urgency from
state. Applies the fashion pricing skill (markdown ladder, trend holds,
psychological pricing). Auto-executes safe actions. Queues risky ones for
human approval.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  fetch_pricing_data       ← Node 1: list_products + calculate_sales_velocity
      │                               + get_price_rules via shopify-mcp.
      │                               Reads inventory_snapshot + trend_signals
      │                               already in state from prior agents.
      ▼
  load_domain_skill        ← Node 2: load_skill("fashion_pricing")
      │
      ▼
  run_claude_analysis      ← Node 3: Structured LLM call.
      │                               Produces PricingDecision list with
      │                               auto_execute flag per recommendation.
      ▼
  execute_pricing_actions  ← Node 4: Executes auto-approved actions via MCP.
      │                               Queues the rest as pending_approval.
      │                               Writes pricing_recommendations + alerts.
      ▼
    END

Auto-execute thresholds:
  🗸 "hold"     → no Shopify call needed
  🗸 "markdown" ≤ 15% AND first markdown (compare_at_price == 0) → execute
  ◔ "markdown" > 15%  → pending_approval (human decides next rung)
  ◔ "increase"         → pending_approval (higher brand risk)
  ◔ "clearance_code"   → pending_approval (creates price rule + discount code)
  ◔ "bundle"           → pending_approval (manual setup needed)

Markdown ladder state via compare_at_price:
  Shopify's compare_at_price = the "was" price shown as a strikethrough.
  When we first markdown a SKU, we set compare_at_price = original price.
  On the next cycle, compare_at_price > 0 tells us we're already on the ladder.
  Current rung = (compare_at_price - current_price) / compare_at_price * 100.

  Rung 0 (fresh):     compare_at_price == 0             → next step: 15% off
  Rung 1 (~15% off):  compare_at_price > 0, discount ≈ 15% → next step: 25% off
  Rung 2 (~25% off):  compare_at_price > 0, discount ≈ 25% → next step: 35–40% + code
  Rung 3 (clearance): discount ≥ 35%                   → dead stock, code needed

Chaining with Inventory Agent:
  Pricing Agent reads state.inventory_snapshot (set by Inventory Agent)
  to get days_of_stock_remaining and urgency per SKU. This avoids a redundant
  MCP call and keeps the agents composable.

Chaining with Trend Agent (future):
  When Trend Agent is built, state.trend_signals will be populated.
  Pricing Agent already reads trend_signals from state — it will automatically
  use them once available. High-signal trending SKUs = hold or increase.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
import operator

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import (
    AgentAlert,
    InventorySnapshot,
    PricingRecommendation,
    TrendSignal,
)


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")


# Auto-execute ceiling: markdowns at or below this % are applied automatically.
# Everything above goes to pending_approval.
AUTO_EXECUTE_MARKDOWN_CEILING_PCT = float(
    os.getenv("AUTO_EXECUTE_MARKDOWN_CEILING_PCT", "15.0")
)

model = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ── Structured output schema ───────────────────────────────────────────────────

class _PricingDecision(BaseModel):
    """One pricing decision per variant SKU."""

    sku:           str
    variant_id:    int
    product_title: str
    variant_title: str

    current_price:     float = Field(ge=0)
    compare_at_price:  float = Field(ge=0, description="0 if SKU is not currently on markdown.")
    recommended_price: float = Field(ge=0)

    action: str = Field(
        description=(
            'One of: "hold" | "markdown" | "increase" | "clearance_code" | "bundle". '
            '"hold" = no price change. '
            '"markdown" = reduce price, set compare_at_price to original. '
            '"increase" = raise price (trending or premium positioning). '
            '"clearance_code" = deepest markdown + create a discount code for the channel. '
            '"bundle" = flag for human to create a bundle with another SKU.'
        )
    )
    discount_pct: float = Field(
        ge=0, le=100,
        description=(
            "Discount percentage from current_price for markdown/clearance actions. "
            "0 for hold and increase actions."
        )
    )
    new_compare_at_price: Optional[float] = Field(
        default=None,
        description=(
            "Value to set as compare_at_price (the 'was' price). "
            "For first markdown: original current_price. "
            "For subsequent rungs: keep the original compare_at_price (don't reset it). "
            "None for hold / increase."
        )
    )

    # Markdown ladder state
    markdown_rung: int = Field(
        default=0,
        description=(
            "Which rung of the markdown ladder this SKU is on AFTER this action. "
            "0 = full price, 1 = 15% off, 2 = 25% off, 3 = 35-40% off / clearance."
        )
    )
    days_since_last_sale: Optional[float] = Field(
        default=None,
        description="Days since the SKU last had any sale. Derived from velocity data."
    )

    # Execution routing
    auto_execute: bool = Field(
        description=(
            "True = execute this action immediately via Shopify API. "
            "False = queue for human approval in the dashboard. "
            "Rule: True ONLY for action='hold' or (action='markdown' AND discount_pct <= 15 "
            "AND markdown_rung == 0). Everything else is False."
        )
    )
    reason: str = Field(
        description=(
            "1-2 sentence explanation. Include: what triggered this, "
            "the relevant numbers (velocity, days unsold, margin). "
            "Example: 'SKU FOS-001 has 0 sales in 52 days (dead stock). "
            "First markdown rung: 15% off sets price from PKR 2999 to PKR 2549.'"
        )
    )

    # For clearance_code action only
    suggested_discount_code: Optional[str] = Field(
        default=None,
        description=(
            "Discount code to create if action is clearance_code. "
            "Format: CLEAR-{SKU_SLUG}-{YYYYMM}. E.g. CLEAR-FOS001-202501. "
            "None for other actions."
        )
    )


class _PricingAnalysis(BaseModel):
    """Complete structured output for one Pricing Agent run."""

    decisions:   list[_PricingDecision]
    summary:     str = Field(
        description=(
            "2–3 sentence overview. Example: '14 SKUs held at full price (trending or healthy). "
            "3 first markdowns auto-executed (15% off). "
            "2 clearance candidates queued for human approval (>45 days dead stock).'"
        )
    )


# ── Subgraph state ─────────────────────────────────────────────────────────────

class PricingAgentState(TypedDict):
    # From parent state (read)
    brand_id:   str
    brand_name: str

    # Read from parent state — set by Inventory Agent earlier in the run
    inventory_snapshot: list[InventorySnapshot]
    trend_signals:      list[TrendSignal]   # empty list until Trend Agent is built

    # Populated by Node 1
    products:            list[dict]
    sales_velocity:      list[dict]
    existing_price_rules: list[dict]

    # Agent-internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → merged into parent FashionOSState
    pricing_recommendations: Annotated[list[PricingRecommendation], operator.add]
    alerts:                  Annotated[list[AgentAlert],             operator.add]


# ── Helper: parse MCP results ──────────────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
    # LangChain content block: [{'type': 'text', 'text': '[...json...]', 'id': '...'}]
    if (
        isinstance(raw, list)
        and len(raw) > 0
        and isinstance(raw[0], dict)
        and "text" in raw[0]
    ):
        return json.loads(raw[0]["text"])

    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    # ToolMessage or similar
    content = getattr(raw, "content", str(raw))
    if isinstance(content, str):
        return json.loads(content)
    return content

# ── Helper: infer markdown rung from Shopify prices ───────────────────────────

def _current_rung(current_price: float, compare_at_price: float) -> int:
    """
    Determines which rung of the markdown ladder a SKU is currently on,
    using Shopify's compare_at_price as the canonical source of truth.

    Rung 0 = not marked down (compare_at_price == 0 or equals current_price)
    Rung 1 = ~15% off
    Rung 2 = ~25% off
    Rung 3 = ~35%+ off (clearance territory)
    """
    if compare_at_price <= 0 or compare_at_price <= current_price:
        return 0
    discount = (compare_at_price - current_price) / compare_at_price * 100
    if discount < 20:
        return 1
    if discount < 30:
        return 2
    return 3


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_pricing_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_pricing_data(state: PricingAgentState) -> dict:
    """
    Fetches three datasets from shopify-mcp:
      1. list_products     → current prices + compare_at_prices (markdown ladder state)
      2. calculate_sales_velocity → 14-day units/day per SKU
      3. get_price_rules   → existing discounts (prevent double-discounting)

    Inventory snapshot is already in state from the Inventory Agent — no need
    to re-fetch. This is the composability benefit of shared state.
    """


    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    products_raw = await tool_map["list_products"].ainvoke(
        {"limit": 250, "status": "active"}
    )
    velocity_raw = await tool_map["calculate_sales_velocity"].ainvoke(
        {"days": 14}
    )
    rules_raw = await tool_map["get_price_rules"].ainvoke(
        {"active_only": True}
    )

    products      = _parse_mcp_result(products_raw)
    velocity      = _parse_mcp_result(velocity_raw)
    price_rules   = _parse_mcp_result(rules_raw)

    print(
        f"[Pricing] Fetched {len(products)} products, "
        f"{len(velocity)} velocity records, "
        f"{len(price_rules)} active price rules."
    )

    return {
        "products":             products,
        "sales_velocity":       velocity,
        "existing_price_rules": price_rules,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: PricingAgentState) -> dict:
    """Loads the fashion_pricing domain skill for use in the analysis prompt."""
    skill = load_skill("fashion_pricing")
    print("[Pricing] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_claude_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_claude_analysis(state: PricingAgentState) -> dict:
    """
    Single structured LLM call that produces the full pricing decision set.

    Input payload (compact, token-efficient):
    - Per-SKU: current_price, compare_at_price, current_markdown_rung,
               units_per_day, days_of_stock_remaining, urgency, is_trending
    - Existing price rules (to prevent double-discounting)
    - Trend signals from state (empty list until Trend Agent is built)

    Not a ReAct loop — all data is in state from Node 1 and from the
    Inventory Agent's prior run. One call is faster and cheaper.
    """
    # ── Build velocity lookup ──────────────────────────────────────────────
    velocity_by_sku: dict[str, float] = {
        v["sku"]: v["units_per_day"]
        for v in state.get("sales_velocity", [])
        if v.get("sku")
    }

    # ── Build inventory lookup from Inventory Agent's state output ─────────
    inv_by_sku: dict[str, InventorySnapshot] = {
        s["sku"]: s
        for s in state.get("inventory_snapshot", [])
        if s.get("sku")
    }

    # ── Build trend lookup ─────────────────────────────────────────────────
    trending_skus: set[str] = {
        sig["matched_sku"]
        for sig in state.get("trend_signals", [])
        if sig.get("matched_sku") and sig.get("direction") in ("rising", "peaking")
    }

    # ── Existing discount codes (flat set for fast lookup) ─────────────────
    existing_rule_titles: list[str] = [
        r.get("title", "")
        for r in state.get("existing_price_rules", [])
    ]

    # ── Build compact payload ──────────────────────────────────────────────
    compact: list[dict] = []
    for product in state.get("products", []):
        for variant in product.get("variants", []):
            sku = (variant.get("sku") or "").strip()
            if not sku:
                continue

            current_price    = variant.get("price", 0.0)
            compare_at_price = variant.get("compare_at_price", 0.0)
            velocity         = velocity_by_sku.get(sku, 0.0)
            inv_snap         = inv_by_sku.get(sku, {})
            rung             = _current_rung(current_price, compare_at_price)

            compact.append({
                "sku":               sku,
                "variant_id":        variant.get("variant_id"),
                "product_title":     product.get("title", ""),
                "variant_title":     variant.get("title", ""),
                "current_price":     current_price,
                "compare_at_price":  compare_at_price,
                "markdown_rung":     rung,
                "units_per_day":     velocity,
                "current_stock":     inv_snap.get("current_stock", variant.get("inventory_quantity", 0)),
                "days_remaining":    inv_snap.get("days_of_stock_remaining", 999.0),
                "urgency":           inv_snap.get("urgency", "unknown"),
                "is_trending":       sku in trending_skus,
                "zero_velocity":     velocity == 0.0,
            })

    if not compact:
        empty = _PricingAnalysis(
            decisions=[],
            summary="No active SKUs with pricing data found.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    # ── Prompts ────────────────────────────────────────────────────────────
    system_prompt = f"""You are the Pricing Agent for {state['brand_name']}, \
an autonomous fashion brand AI. You make data-driven pricing decisions every time \
the supervisor runs.

{state['skill_content']}

## Decision framework (apply in this exact order)

### 1. Trending items (is_trending = true OR units_per_day ≥ 3x store average)
- Action: "hold" or "increase" (5–10%)
- NEVER mark down a trending item. Price inelasticity + scarcity = hold margin.
- Only increase if current gross margin ≥ 60% AND stock > 20 units.

### 2. Healthy, non-trending items (units_per_day > 0, urgency = "healthy" or "normal")
- Action: "hold"
- No action needed.

### 3. Slow-moving items (units_per_day < 0.3 AND zero_velocity = false)
- Action: "hold" for now — watch for 2 more cycles before marking down.
- Exception: if markdown_rung > 0 already, continue the ladder.

### 4. Dead stock — first markdown (zero_velocity = true AND markdown_rung = 0)
- Days unsold proxy: if units_per_day == 0 and stock > 0, assume dead stock.
- If current_stock > 5: action = "markdown", discount_pct = 15
  - auto_execute = True (first rung, low risk, fully reversible)
  - new_compare_at_price = current_price (the "was" price)
  - recommended_price = round to nearest 99 or 499 (psychological pricing rule)
- If current_stock ≤ 5: action = "hold" (not worth discounting so few units)

### 5. Dead stock — second markdown (zero_velocity = true AND markdown_rung = 1)
- Action: "markdown", discount_pct = 25
- auto_execute = False → pending_approval (higher markdown = human reviews)
- Keep the ORIGINAL compare_at_price (don't reset it — customer sees the full journey)

### 6. Dead stock — clearance (zero_velocity = true AND markdown_rung = 2)
- Action: "clearance_code", discount_pct = 35 (or 40 if urgency = "critical")
- auto_execute = False → pending_approval
- Generate a suggested_discount_code following the convention

### 7. Critical stockout risk (urgency = "critical")
- If is_trending OR units_per_day > 2: action = "hold" (don't discount a selling-out item)
- Otherwise: no pricing action from this agent (Restock Agent handles it)

## Psychological pricing rule (ALWAYS apply to recommended_price)
- End in 99 or 499. Never end in 0, 5, or other values.
- PKR 2549 → use 2499. PKR 2860 → use 2899. PKR 3180 → use 3199.
- Exception: if the result would push discount_pct above the target by more than 2%,
  use the higher 99 ending instead.

## Double-discount prevention
The following price rules are already active in Shopify. Do NOT create a
clearance_code action for any SKU whose title appears in an existing rule:
{json.dumps(existing_rule_titles, indent=2)}

## Output rules
- Include EVERY active SKU in decisions. Even "hold" decisions must appear.
- auto_execute = True ONLY for: action="hold" OR (action="markdown" AND discount_pct <= 15 AND markdown_rung == 0)
- Everything else: auto_execute = False
"""

    user_msg = (
        f"Here is the current pricing + inventory snapshot for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Analyse every SKU and return your complete structured pricing decisions."
    )

    structured_llm = model.with_structured_output(_PricingAnalysis)
    analysis: _PricingAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    auto_count    = sum(1 for d in analysis.decisions if d.auto_execute)
    pending_count = sum(1 for d in analysis.decisions if not d.auto_execute and d.action != "hold")

    print(
        f"[Pricing] Analysis complete. "
        f"{len(analysis.decisions)} decisions: "
        f"{auto_count} auto-execute, {pending_count} pending approval. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_pricing_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_pricing_actions(state: PricingAgentState) -> dict:
    """
    Executes auto-approved decisions immediately via shopify-mcp.
    Queues all others as PricingRecommendation(status="pending_approval").

    Auto-execute = update_product_price() via MCP.
    Pending      = write to state.pricing_recommendations for dashboard display.

    All decisions (executed or pending) are written to state.pricing_recommendations
    so the dashboard can show the full picture.
    """
    analysis   = _PricingAnalysis.model_validate_json(state["raw_analysis"])
    now_iso    = datetime.now(timezone.utc).isoformat()
    auto_count = 0
    fail_count = 0

    pricing_recommendations: list[PricingRecommendation] = []
    alerts:                  list[AgentAlert]             = []

    # ── Open MCP connection for writes ────────────────────────────────────
    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    for d in analysis.decisions:
        # ── Build the canonical PricingRecommendation for state ────────
        rec = PricingRecommendation(
            sku              = d.sku,
            variant_id       = d.variant_id,
            current_price    = d.current_price,
            recommended_price= d.recommended_price,
            action           = d.action,
            discount_pct     = d.discount_pct,
            reason           = d.reason,
        )

        # ── Auto-execute path ──────────────────────────────────────────
        if d.auto_execute and d.action == "markdown":
            try:
                await tool_map["update_product_price"].ainvoke({
                    "variant_id":       d.variant_id,
                    "new_price":        d.recommended_price,
                    "compare_at_price": d.new_compare_at_price,
                    "reason":           f"[AUTO] {d.reason}",
                })
                auto_count += 1
                print(
                    f"[Pricing] ✅ Auto-executed {d.action} on {d.sku}: "
                    f"PKR {d.current_price} → PKR {d.recommended_price} "
                    f"({d.discount_pct:.0f}% off)"
                )

                # Raise an INFO alert so the run summary mentions it
                alerts.append(AgentAlert(
                    level      = "info",
                    agent      = "pricing_agent",
                    message    = (
                        f"Auto-executed {d.discount_pct:.0f}% markdown on {d.sku} "
                        f"({d.product_title} / {d.variant_title}): "
                        f"PKR {d.current_price:.0f} → PKR {d.recommended_price:.0f}."
                    ),
                    sku        = d.sku,
                    created_at = now_iso,
                ))

            except Exception as exc:
                fail_count += 1
                print(f"[Pricing] ❌ Failed to execute on {d.sku}: {exc}")
                alerts.append(AgentAlert(
                    level      = "warning",
                    agent      = "pricing_agent",
                    message    = f"Price update FAILED for {d.sku}: {exc}",
                    sku        = d.sku,
                    created_at = now_iso,
                ))

        # ── Pending-approval path ──────────────────────────────────────
        elif not d.auto_execute and d.action not in ("hold",):
            # Write to state as pending — dashboard shows it for human review
            print(
                f"[Pricing] ◔ Queued for approval: {d.action} on {d.sku} "
                f"({d.discount_pct:.0f}% → PKR {d.recommended_price:.0f})"
            )

            # Raise a warning alert for pending markdowns >15% so supervisor notices
            if d.action in ("markdown", "clearance_code") and d.discount_pct > 15:
                alerts.append(AgentAlert(
                    level      = "warning",
                    agent      = "pricing_agent",
                    message    = (
                        f"PENDING APPROVAL: {d.discount_pct:.0f}% {d.action} "
                        f"on {d.sku} ({d.product_title} / {d.variant_title}). "
                        f"Recommended price: PKR {d.recommended_price:.0f}. "
                        f"Reason: {d.reason}"
                    ),
                    sku        = d.sku,
                    created_at = now_iso,
                ))

            if d.action == "increase":
                alerts.append(AgentAlert(
                    level      = "info",
                    agent      = "pricing_agent",
                    message    = (
                        f"PENDING APPROVAL: Price increase on {d.sku} "
                        f"({d.product_title}). "
                        f"Suggested: PKR {d.current_price:.0f} → "
                        f"PKR {d.recommended_price:.0f}. "
                        f"Reason: {d.reason}"
                    ),
                    sku        = d.sku,
                    created_at = now_iso,
                ))

        # All decisions (hold, auto-executed, pending) go into state
        pricing_recommendations.append(rec)

    print(
        f"[Pricing] Done. {auto_count} auto-executed, "
        f"{fail_count} failed, "
        f"{len([d for d in analysis.decisions if not d.auto_execute and d.action != 'hold'])} pending."
    )

    return {
        "pricing_recommendations": pricing_recommendations,
        "alerts":                  alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_pricing_graph() -> StateGraph:
    graph = StateGraph(PricingAgentState)

    graph.add_node("fetch_pricing_data",     fetch_pricing_data)
    graph.add_node("load_domain_skill",      load_domain_skill)
    graph.add_node("run_claude_analysis",    run_claude_analysis)
    graph.add_node("execute_pricing_actions",execute_pricing_actions)

    graph.add_edge(START,                     "fetch_pricing_data")
    graph.add_edge("fetch_pricing_data",      "load_domain_skill")
    graph.add_edge("load_domain_skill",       "run_claude_analysis")
    graph.add_edge("run_claude_analysis",     "execute_pricing_actions")
    graph.add_edge("execute_pricing_actions", END)

    return graph.compile()


pricing_graph = build_pricing_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.pricing.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Pricing Agent Test Run")
        print("═" * 60 + "\n")

        initial_state: PricingAgentState = {
            "brand_id":               os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":             os.getenv("BRAND_NAME", "TestBrand"),
            # Simulate Inventory Agent having already run
            "inventory_snapshot":     [],
            "trend_signals":          [],
            "products":               [],
            "sales_velocity":         [],
            "existing_price_rules":   [],
            "skill_content":          "",
            "raw_analysis":           "",
            "pricing_recommendations":[],
            "alerts":                 [],
        }

        result = await pricing_graph.ainvoke(initial_state)

        ACTION_EMOJI = {
            "hold":           "🔵",
            "markdown":       "🟡",
            "increase":       "🟢",
            "clearance_code": "🔴",
            "bundle":         "🟣",
        }
        EXEC_LABEL = {True: "AUTO", False: "PENDING"}

        print("\n── PRICING DECISIONS ──────────────────────────────────────────")
        for rec in result["pricing_recommendations"]:
            action  = rec.get("action", "hold")
            emoji   = ACTION_EMOJI.get(action, "⚪")
            change  = rec["recommended_price"] - rec["current_price"]
            sign    = "+" if change >= 0 else ""
            print(
                f"  {emoji} {rec['sku']:<20} "
                f"{action.upper():<16} "
                f"PKR {rec['current_price']:>6.0f} → {rec['recommended_price']:>6.0f} "
                f"({sign}{change:.0f})"
            )

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            icon = {"critical": "🚨", "warning": "⚠️ ", "info": "ℹ️ "}.get(alert["level"], "  ")
            print(f"  {icon} {alert['level'].upper()} [{alert.get('sku', '—')}]: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())