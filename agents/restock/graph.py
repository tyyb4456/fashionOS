"""
Restock Agent — FashionOS Phase 2 Operations
=============================================
Reads inventory_snapshot and pricing_recommendations already in state
(set by Inventory Agent and Pricing Agent). Identifies SKUs that need
restocking — critical/high urgency items that aren't being clearanced.
Calculates recommended order quantities using supplier lead times from
the fashion_inventory skill. Generates supplier WhatsApp messages.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  prepare_restock_data     ← Node 1: NO MCP — reads inventory_snapshot +
      │                               pricing_recommendations from state.
      │                               Filters to restock candidates.
      ▼
  load_domain_skill        ← Node 2: load_skill("fashion_inventory")
      │                               Contains supplier lead time data.
      ▼
  run_claude_analysis      ← Node 3: Structured LLM call.
      │                               Produces RestockDecision list with
      │                               quantity, supplier type, WhatsApp message.
      ▼
  execute_restock_actions  ← Node 4: Calls create_restock_recommendation
      │                               via shopify-mcp for each decision.
      │                               Writes restock_recommendations + alerts.
      ▼
    END

Key design decisions:
  - Node 1 is pure Python (no MCP) — data is already in state from prior agents.
    This is the composability benefit of the shared FashionOSState pattern.
  - Skip clearance SKUs: if Pricing Agent has already decided to clear a SKU,
    restocking it would be contradictory. We read pricing_action per SKU and
    exclude clearance_code decisions from the restock candidates.
  - ALL restock recommendations are pending_approval — no auto-ordering.
    Humans approve every purchase order in the dashboard. This is a deliberate
    trust boundary (money leaving the business requires human sign-off).
  - Supplier WhatsApp messages are generated in Urdu-English mix (Pakistani
    supplier context) — ready to paste into WhatsApp without editing.

Quantity formula:
  recommended_qty = ceil(units_per_day × (lead_time + 7)) - current_stock
  Where:
    lead_time = supplier type lead time in days
    7         = safety buffer days
  Minimum order: 20 units (MOQ floor)
  Maximum order: units_per_day × 60 (2-month supply cap)

Supplier lead times:
  lahore_local    →  10 days  (fastest, default for PK fashion items)
  karachi_trader  →   7 days  (basics/staples, slightly faster)
  china_import    →  32 days  (accessories, incl. ~5-7 day customs buffer)

Chaining:
  Runs AFTER Inventory Agent (inventory_snapshot)
  Runs AFTER Pricing Agent   (pricing_recommendations — used to skip clearance)
  Supervisor: inventory → pricing → restock → summarize

Standalone test:
  python -m agents.restock.graph
"""

import json
import os
from datetime import datetime, timezone
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
    RestockRecommendation,
)
from response_schemas.restock_model import RestockAnalysis

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ── Subgraph state ─────────────────────────────────────────────────────────────

class RestockAgentState(TypedDict):
    # From parent state (read-only context)
    brand_id:   str
    brand_name: str

    # Read from prior agents — already in state, no MCP fetch needed
    inventory_snapshot:      list[InventorySnapshot]
    pricing_recommendations: list[PricingRecommendation]

    # Populated by Node 1 (internal scratch — not in FashionOSState, LangGraph drops on merge)
    restock_candidates: list[dict]

    # Agent-internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → merged into parent FashionOSState via operator.add
    restock_recommendations: Annotated[list[RestockRecommendation], operator.add]
    alerts:                  Annotated[list[AgentAlert],             operator.add]


# ── Helper: parse MCP results ──────────────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
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
    content = getattr(raw, "content", str(raw))
    if isinstance(content, str):
        return json.loads(content)
    return content


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — prepare_restock_data
# ══════════════════════════════════════════════════════════════════════════════

def prepare_restock_data(state: RestockAgentState) -> dict:
    """
    Reads inventory_snapshot + pricing_recommendations from state.
    No MCP connection — data is already present from prior agents.

    Filters to restock candidates:
      - Only "critical" and "high" urgency SKUs (from Inventory Agent)
      - Annotates each with the pricing action (from Pricing Agent)
        so the LLM can skip clearance SKUs without extra context
      - Skips SKUs with zero velocity (dead stock → Pricing Agent handles those)

    The combined payload goes into state.restock_candidates for Node 3 to analyse.
    This is the composability pattern: two agents' outputs combined in a third
    without any additional I/O.
    """
    # Build pricing lookup by SKU
    pricing_by_sku: dict[str, str] = {
        p["sku"]: p.get("action", "hold")
        for p in state.get("pricing_recommendations", [])
        if p.get("sku")
    }

    candidates: list[dict] = []

    for snap in state.get("inventory_snapshot", []):
        sku     = snap.get("sku", "")
        urgency = snap.get("urgency", "healthy")

        if urgency not in ("critical", "high"):
            continue

        velocity = snap.get("units_per_day", 0.0)
        pricing_action = pricing_by_sku.get(sku, "hold")

        candidates.append({
            "sku":                     sku,
            "product_title":           snap.get("product_title", ""),
            "variant_title":           snap.get("variant_title", ""),
            "current_stock":           snap.get("current_stock", 0),
            "units_per_day":           velocity,
            "days_of_stock_remaining": snap.get("days_of_stock_remaining", 0.0),
            "urgency":                 urgency,
            "pricing_action":          pricing_action,
            # Pre-flag for the LLM — clearance = don't restock
            "is_on_clearance":         pricing_action == "clearance_code",
            # Zero velocity = dead stock — pricing handles it, not us
            "zero_velocity":           velocity == 0.0,
        })

    n_critical = sum(1 for c in candidates if c["urgency"] == "critical")
    n_high     = sum(1 for c in candidates if c["urgency"] == "high")
    n_clearance = sum(1 for c in candidates if c["is_on_clearance"])

    print(
        f"[Restock] {len(candidates)} candidates "
        f"({n_critical} critical, {n_high} high, {n_clearance} flagged for clearance)."
    )

    return {"restock_candidates": candidates}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: RestockAgentState) -> dict:
    """
    Loads the fashion_inventory domain skill.

    This skill contains:
    - Supplier lead times for Pakistan (Lahore, Karachi, China)
    - Dead stock thresholds and urgency definitions
    - Seasonal context (Eid, summer, winter peaks)
    - Pakistani size distribution patterns

    Loaded here so Node 3's system prompt has full domain context without
    bloating the base system prompt.
    """
    skill = load_skill("fashion_inventory")
    print("[Restock] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_claude_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_claude_analysis(state: RestockAgentState) -> dict:
    """
    Single structured LLM call that produces restock decisions for all candidates.

    For each candidate:
    - Decides whether to restock (skips clearance and zero-velocity SKUs)
    - Calculates recommended order quantity using the lead-time formula
    - Selects the appropriate supplier type for the product category
    - Computes expected stockout date
    - Generates a WhatsApp-ready supplier message in Urdu-English

    Not a ReAct loop — all data is in state. One structured call is sufficient.
    """
    candidates = state.get("restock_candidates", [])

    if not candidates:
        print("[Restock] No candidates — skipping analysis.")
        empty = RestockAnalysis(
            decisions=[],
            summary="No SKUs require restocking this cycle. All inventory is healthy or managed by pricing.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    system_prompt = f"""You are the Restock Agent for {state['brand_name']}, \
an autonomous AI fashion brand operating system.

{state['skill_content']}

## Your task
Analyse the restock candidates and decide whether each SKU needs a purchase order.
Today's date is {today}.

## Quantity formula
recommended_qty = ceil(units_per_day × (lead_time + 7)) - current_stock

Where:
  lead_time = supplier lead time in days (see supplier types below)
  7         = safety buffer days (ensures stock on arrival before next stockout)
  result    = round UP (ceil), then apply floor/cap

Rules:
  - If result ≤ 0: no restock needed (stock will last through lead time)
  - If 0 < result < 20: order 20 (minimum MOQ floor)
  - If result > units_per_day × 60: cap at units_per_day × 60 (no over-ordering)

## Supplier types
| Type              | Lead days | Use for                                              |
|-------------------|-----------|------------------------------------------------------|
| lahore_local      | 10        | Pakistani women's fashion (kurtas, suits, lawn, co-ords, shalwar kameez) |
| karachi_trader    | 7         | Basics and staples (plain fabric, simple cuts, essentials) |
| china_import      | 32        | Accessories, bags, shoes, jewelry, novelty items     |

CRITICAL RULE: NEVER recommend china_import for urgency="critical" — lead time is too long.
Default to lahore_local unless the product is clearly accessories/import-only.

## Decision rules

### should_restock = False when:
  1. is_on_clearance = True → Pricing Agent is clearing this stock; contradictory to restock
  2. zero_velocity = True → Dead stock; no point ordering more of something not selling
  3. Formula result ≤ 0 → Stock covers the full lead time + buffer already

### should_restock = True when:
  - urgency = "critical" or "high"
  - is_on_clearance = False
  - zero_velocity = False
  - Formula result > 0

## expected_stockout_date
Compute: {today} + floor(days_of_stock_remaining) days.
Format: YYYY-MM-DD.

## Supplier message style
Write as a Pakistani fashion brand would actually WhatsApp a local supplier:
- Natural Urdu-English mix (code-switching is normal in Pakistani business)
- Warm but businesslike — suppliers are partners, not just vendors
- Be specific: include SKU, product name, exact quantity, urgency, delivery deadline
- Always request price confirmation (even for known suppliers)
- Keep under 200 words

## Output requirement
Include ALL candidates in decisions — either should_restock=True or False with skip_reason.
Never omit a candidate from the output.
"""

    user_msg = (
        f"Restock candidates for {state['brand_name']} (today: {today}):\n\n"
        f"```json\n{json.dumps(candidates, indent=2)}\n```\n\n"
        "Produce restock decisions with supplier messages for all candidates above."
    )

    structured_llm = model.with_structured_output(RestockAnalysis)
    analysis: RestockAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    restock_count  = sum(1 for d in analysis.decisions if d.should_restock)
    skip_count     = sum(1 for d in analysis.decisions if not d.should_restock)
    total_units    = sum(d.recommended_quantity for d in analysis.decisions if d.should_restock)

    print(
        f"[Restock] Analysis complete. "
        f"{restock_count} orders, {skip_count} skipped, "
        f"{total_units} total units. Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_restock_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_restock_actions(state: RestockAgentState) -> dict:
    """
    For each should_restock=True decision, calls create_restock_recommendation
    via shopify-mcp to register the pending order.

    ALL restock recommendations are pending_approval — this agent never
    auto-orders. Every purchase order requires explicit human approval in
    the dashboard. This is a deliberate trust boundary (real money).

    Writes to state.restock_recommendations + state.alerts.
    Critical urgency → "critical" alert. High urgency → "warning" alert.
    Both levels surface in the dashboard and run summary.
    """
    analysis = RestockAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    restock_recommendations: list[RestockRecommendation] = []
    alerts:                  list[AgentAlert]             = []

    orders_to_create = [d for d in analysis.decisions if d.should_restock]

    if not orders_to_create:
        print("[Restock] No restock orders to create this cycle.")
        return {"restock_recommendations": [], "alerts": []}

    # ── Open MCP connection ────────────────────────────────────────────────
    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    if "create_restock_recommendation" not in tool_map:
        print("[Restock] WARNING: 'create_restock_recommendation' not in tool_map — rebuild Docker image")

    for d in analysis.decisions:
        if not d.should_restock:
            print(f"[Restock] ◔ Skipped {d.sku}: {d.skip_reason}")
            continue

        # ── Call MCP tool ─────────────────────────────────────────────────
        if "create_restock_recommendation" in tool_map:
            try:
                raw = await tool_map["create_restock_recommendation"].ainvoke({
                    "sku":                     d.sku,
                    "recommended_quantity":    d.recommended_quantity,
                    "urgency":                 d.urgency,
                    "days_of_stock_remaining": d.days_of_stock_remaining,
                    "units_per_day":           d.units_per_day,
                    "reason":                  d.reason,
                    "supplier_message":        d.supplier_message,
                    "brand_id": state["brand_id"],
                })
                _parse_mcp_result(raw)  # validate parseable; result not used directly
            except Exception as exc:
                # Non-fatal — we still write to state even if MCP call fails
                print(f"[Restock] ⚠ MCP call failed for {d.sku}: {exc}")

        # ── Build typed RestockRecommendation ─────────────────────────────
        rec = RestockRecommendation(
            sku                     = d.sku,
            recommended_quantity    = d.recommended_quantity,
            urgency                 = d.urgency,
            days_of_stock_remaining = d.days_of_stock_remaining,
            units_per_day           = d.units_per_day,
            reason                  = d.reason,
            supplier_message        = d.supplier_message,
            status                  = "pending_approval",
        )
        restock_recommendations.append(rec)

        # ── Raise alert ───────────────────────────────────────────────────
        alert_level = "critical" if d.urgency == "critical" else "warning"
        alerts.append(AgentAlert(
            level      = alert_level,
            agent      = "restock_agent",
            message    = (
                f"RESTOCK PENDING APPROVAL: {d.sku} "
                f"({d.product_title} / {d.variant_title}). "
                f"{d.days_of_stock_remaining:.1f} days remaining at "
                f"{d.units_per_day:.1f} units/day. "
                f"Order {d.recommended_quantity} units via {d.supplier_type} "
                f"({d.estimated_lead_days}d lead). "
                f"Stockout: {d.expected_stockout_date}."
            ),
            sku        = d.sku,
            created_at = now_iso,
        ))

        print(
            f"[Restock] 🗸 Queued {d.sku}: "
            f"{d.recommended_quantity} units | "
            f"{d.urgency.upper()} | "
            f"{d.supplier_type} ({d.estimated_lead_days}d) | "
            f"stockout {d.expected_stockout_date}"
        )

    print(
        f"[Restock] Done. {len(restock_recommendations)} orders pending approval, "
        f"{len(alerts)} alerts raised."
    )

    return {
        "restock_recommendations": restock_recommendations,
        "alerts":                  alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_restock_graph() -> StateGraph:
    """
    Assembles and compiles the Restock Agent subgraph.

    Returns a compiled LangGraph usable two ways:
    1. Standalone test:  await restock_graph.ainvoke(initial_state)
    2. Inside supervisor: run_restock_agent() calls it after Pricing Agent
    """
    graph = StateGraph(RestockAgentState)

    graph.add_node("prepare_restock_data",    prepare_restock_data)
    graph.add_node("load_domain_skill",       load_domain_skill)
    graph.add_node("run_claude_analysis",     run_claude_analysis)
    graph.add_node("execute_restock_actions", execute_restock_actions)

    graph.add_edge(START,                      "prepare_restock_data")
    graph.add_edge("prepare_restock_data",     "load_domain_skill")
    graph.add_edge("load_domain_skill",        "run_claude_analysis")
    graph.add_edge("run_claude_analysis",      "execute_restock_actions")
    graph.add_edge("execute_restock_actions",  END)

    return graph.compile()


restock_graph = build_restock_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.restock.graph
# (uses mock inventory + pricing state — no live Shopify data needed for Node 1)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Restock Agent Test Run")
        print("═" * 60 + "\n")

        # Simulate Inventory Agent + Pricing Agent having already run
        mock_inventory: list[InventorySnapshot] = [
            {
                "sku":                     "FOS-001-S",
                "product_title":           "Olive Cargo Pants",
                "variant_title":           "Small",
                "current_stock":           8,
                "units_per_day":           1.8,
                "days_of_stock_remaining": 4.4,
                "urgency":                 "critical",
            },
            {
                "sku":                     "FOS-001-M",
                "product_title":           "Olive Cargo Pants",
                "variant_title":           "Medium",
                "current_stock":           15,
                "units_per_day":           1.2,
                "days_of_stock_remaining": 12.5,
                "urgency":                 "high",
            },
            {
                "sku":                     "FOS-002-S",
                "product_title":           "Beige Linen Kurta",
                "variant_title":           "Small",
                "current_stock":           40,
                "units_per_day":           0.0,
                "days_of_stock_remaining": 999.0,
                "urgency":                 "normal",   # healthy — should not appear in candidates
            },
            {
                "sku":                     "FOS-003-M",
                "product_title":           "Pink Chiffon Dupatta",
                "variant_title":           "Free Size",
                "current_stock":           5,
                "units_per_day":           0.0,
                "days_of_stock_remaining": 999.0,
                "urgency":                 "high",     # high urgency but zero velocity + clearance
            },
        ]

        mock_pricing: list[PricingRecommendation] = [
            {
                "sku":               "FOS-001-S",
                "variant_id":        123456,
                "current_price":     2999.0,
                "recommended_price": 2999.0,
                "action":            "hold",
                "discount_pct":      0.0,
                "reason":            "High velocity — hold price.",
            },
            {
                "sku":               "FOS-001-M",
                "variant_id":        123457,
                "current_price":     2999.0,
                "recommended_price": 2999.0,
                "action":            "hold",
                "discount_pct":      0.0,
                "reason":            "Selling well — hold.",
            },
            {
                "sku":               "FOS-003-M",
                "variant_id":        123458,
                "current_price":     1499.0,
                "recommended_price": 899.0,
                "action":            "clearance_code",
                "discount_pct":      40.0,
                "reason":            "Dead stock 60+ days — clearance.",
            },
        ]

        initial_state: RestockAgentState = {
            "brand_id":               os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":             os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":     mock_inventory,
            "pricing_recommendations":mock_pricing,
            "restock_candidates":     [],
            "skill_content":          "",
            "raw_analysis":           "",
            "restock_recommendations":[],
            "alerts":                 [],
        }

        result = await restock_graph.ainvoke(initial_state)

        print("\n── RESTOCK ORDERS ─────────────────────────────────────────────")
        if result["restock_recommendations"]:
            for rec in result["restock_recommendations"]:
                print(
                    f"  {rec['sku']:<20} "
                    f"{rec['urgency'].upper():<10} "
                    f"Order {rec['recommended_quantity']:>4} units  "
                    f"({rec['days_of_stock_remaining']:.1f} days remaining)"
                )
                print(f"    Reason: {rec['reason']}")
        else:
            print("  No restock orders this cycle.")

        print("\n── SUPPLIER MESSAGES ──────────────────────────────────────────")
        for rec in result["restock_recommendations"]:
            print(f"\n  [{rec['sku']}]")
            print(f"  {rec['supplier_message']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert.get('sku', '—')}]"
            print(f"  {alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())