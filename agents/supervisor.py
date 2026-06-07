"""
FashionOS Supervisor Agent
==========================
The central orchestrator. Every agent run passes through here.

Responsibilities:
  1. Inspect the incoming trigger + payload
  2. Decide which Phase 2 agents to activate (routing)
  3. Execute selected agent subgraphs (sequentially)
  4. Write a human-readable run_summary to state

Graph topology:

    START
      │
      ▼
  decide_agents          ← Reads trigger → sets agents_to_run
      │
      ▼
  run_inventory_agent    ← Inventory subgraph ✓
      │                    Also propagates products → parent state
      ▼
  run_trend_agent        ← Trend subgraph ✓  (reads products from state)
      │                    Writes trend_signals BEFORE Pricing runs
      ▼
  run_pricing_agent      ← Pricing subgraph ✓ (reads inventory_snapshot + trend_signals)
      │
      ▼
  run_restock_agent      ← Restock subgraph ✓ (reads inventory + pricing from state)
      │
      ▼
  summarize              ← Writes run_summary + completed_at
      │
      ▼
    END

ORDERING NOTE — Trend Agent MUST run before Pricing Agent:
  Pricing Agent reads state.trend_signals to decide hold vs markdown.
  If Trend ran after Pricing (as v4 handoff doc suggested), those signals
  would be unavailable. inventory → trend → pricing → restock is the
  correct composable order.

Routing table:
  ┌─────────────────────────────┬─────────────────────────────────────────────┐
  │ Trigger                     │ Agents activated                            │
  ├─────────────────────────────┼─────────────────────────────────────────────┤
  │ shopify_webhook (orders/*)  │ inventory → pricing → restock               │
  │ shopify_webhook (inventory) │ inventory only                              │
  │ shopify_webhook (products)  │ inventory only                              │
  │ scheduled_run (hourly)      │ inventory only                              │
  │ scheduled_run (daily)       │ inventory → trend → pricing → restock       │
  │ manual                      │ agents_to_run payload or all four           │
  └─────────────────────────────┴─────────────────────────────────────────────┘

  Trend Agent is NOT on order webhooks — Apify scraping costs quota and
  trends don't change per-order. Pricing Agent handles empty trend_signals
  gracefully (no trending_skus → falls back to velocity-only decisions).
"""

import os
import uuid
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.inventory.graph import inventory_graph
from agents.pricing.graph   import pricing_graph
from agents.restock.graph   import restock_graph
from agents.trend.graph     import trend_graph
from agents.state           import FashionOSState

from langchain.chat_models import init_chat_model


llm = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — decide_agents
# ══════════════════════════════════════════════════════════════════════════════

def decide_agents(state: FashionOSState) -> dict:
    """
    Pure routing node — no LLM call, just deterministic logic.

    Reads trigger + trigger_payload → sets agents_to_run for this run.

    Agent registry:
      "inventory"  → agents/inventory/graph.py  ✓ BUILT
      "trend"      → agents/trend/graph.py      ✓ BUILT
      "pricing"    → agents/pricing/graph.py    ✓ BUILT
      "restock"    → agents/restock/graph.py    ✓ BUILT
      "content"    → agents/content/graph.py    ✗ TODO
      "marketing"  → agents/marketing/graph.py  ✗ TODO
      "dm"         → agents/dm/graph.py         ✗ TODO
      "returns"    → agents/returns/graph.py    ✗ TODO
    """
    trigger         = state.get("trigger", "manual")
    trigger_payload = state.get("trigger_payload", {})

    if trigger == "shopify_webhook":
        topic = trigger_payload.get("topic", "")

        if topic.startswith("orders/"):
            # Order happened → velocity changed → pricing + restock need to react.
            # NOT running Trend here — Apify costs quota, trends don't change per order.
            agents = ["inventory", "pricing", "restock"]
            reasoning = (
                f"Shopify order webhook ({topic}). "
                "Inventory refreshes velocity + stockout risk. "
                "Pricing re-evaluates markdowns with fresh data. "
                "Restock checks if critical SKUs need orders. "
                "Trend Agent skipped — trends don't change per order."
            )

        elif topic.startswith("inventory_levels/"):
            agents = ["inventory"]
            reasoning = (
                f"Inventory level adjustment ({topic}). "
                "Refreshing days-of-stock-remaining only."
            )

        elif topic.startswith("products/"):
            agents = ["inventory"]
            reasoning = (
                f"Product change ({topic}). Refreshing inventory snapshot."
            )

        else:
            agents = ["inventory"]
            reasoning = (
                f"Unknown webhook topic '{topic}'. Running inventory as safe default."
            )

    elif trigger == "scheduled_run":
        schedule_type = trigger_payload.get("schedule_type", "daily")

        if schedule_type == "hourly":
            agents = ["inventory"]
            reasoning = "Hourly sweep: inventory velocity refresh only."

        elif schedule_type == "daily":
            # Full daily sweep — all four operational agents.
            # Trend runs BEFORE Pricing so trend_signals are in state for Pricing to read.
            agents = ["inventory", "trend", "pricing", "restock"]
            reasoning = (
                "Daily full sweep: inventory → trend → pricing → restock. "
                "Trend Agent scrapes TikTok/IG + Google Trends. "
                "Pricing reads trend_signals to hold trending SKUs at full price."
            )

        else:
            agents = ["inventory"]
            reasoning = f"Scheduled run (type={schedule_type}): defaulting to inventory."

    elif trigger == "manual":
        manual_agents = state.get("agents_to_run", [])
        agents    = manual_agents if manual_agents else ["inventory", "trend", "pricing", "restock"]
        reasoning = (
            f"Manual trigger. Running: {', '.join(agents)}."
            if manual_agents
            else "Manual trigger — running full pipeline (inventory, trend, pricing, restock)."
        )

    else:
        agents    = ["inventory"]
        reasoning = f"Unknown trigger '{trigger}'. Defaulting to inventory sweep."

    print(f"[Supervisor] Trigger: {trigger} → Agents: {agents}")
    print(f"[Supervisor] Reasoning: {reasoning}")

    return {
        "agents_to_run":        agents,
        "completed_agents":     [],
        "supervisor_reasoning": reasoning,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — run_inventory_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_inventory_agent(state: FashionOSState) -> dict:
    """
    Runs the Inventory Agent subgraph.

    PROPAGATION FIX: Also returns products to parent FashionOSState so
    the Trend Agent (next node) can use the catalog for SKU matching
    without making a redundant Shopify MCP call.
    """
    if "inventory" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Inventory Agent…")

    subgraph_input = {
        "brand_id":           state["brand_id"],
        "brand_name":         state["brand_name"],
        "products":           state.get("products", []),
        "sales_velocity":     state.get("sales_velocity", []),
        "skill_content":      "",
        "raw_analysis":       "",
        "inventory_snapshot": [],
        "alerts":             [],
    }

    result = await inventory_graph.ainvoke(subgraph_input)

    print(
        f"[Supervisor] ✓ Inventory done. "
        f"{len(result['inventory_snapshot'])} snapshots, "
        f"{len(result['alerts'])} alerts, "
        f"{len(result.get('products', []))} products propagated to state."
    )

    return {
        "inventory_snapshot": result["inventory_snapshot"],
        "products":           result.get("products", []),   # ← propagated for Trend Agent
        "alerts":             result["alerts"],
        "completed_agents":   state.get("completed_agents", []) + ["inventory"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_trend_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_trend_agent(state: FashionOSState) -> dict:
    """
    Runs the Trend Agent subgraph.

    Receives the product catalog from state (populated + propagated by
    run_inventory_agent above). Uses it for SKU matching during analysis.

    Writes trend_signals to state BEFORE Pricing Agent runs — this is the
    key composability win of running Trend second in the chain.
    """
    if "trend" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Trend Agent…")

    subgraph_input = {
        "brand_id":      state["brand_id"],
        "brand_name":    state["brand_name"],
        "products":      state.get("products", []),    # ← from Inventory Agent propagation
        "social_signals":[],
        "trend_data":    [],
        "skill_content": "",
        "raw_analysis":  "",
        "trend_signals": [],
        "alerts":        [],
    }

    result = await trend_graph.ainvoke(subgraph_input)

    rising  = [s for s in result["trend_signals"] if s.get("direction") == "rising"]
    matched = [s for s in result["trend_signals"] if s.get("matched_sku")]

    print(
        f"[Supervisor] ✓ Trend done. "
        f"{len(result['trend_signals'])} signals: "
        f"{len(rising)} rising, {len(matched)} catalog-matched, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "trend_signals":    result["trend_signals"],
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["trend"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — run_pricing_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_pricing_agent(state: FashionOSState) -> dict:
    """
    Runs the Pricing Agent subgraph.

    Runs AFTER Trend Agent so state.trend_signals is populated.
    Pricing Agent reads trending_skus from trend_signals to hold/increase
    prices instead of marking them down.

    Runs AFTER Inventory Agent so state.inventory_snapshot is populated.
    """
    if "pricing" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Pricing Agent…")

    subgraph_input = {
        "brand_id":               state["brand_id"],
        "brand_name":             state["brand_name"],
        "inventory_snapshot":     state.get("inventory_snapshot", []),
        "trend_signals":          state.get("trend_signals", []),   # ← from Trend Agent
        "products":               [],
        "sales_velocity":         [],
        "existing_price_rules":   [],
        "skill_content":          "",
        "raw_analysis":           "",
        "pricing_recommendations":[],
        "alerts":                 [],
    }

    result = await pricing_graph.ainvoke(subgraph_input)

    auto_executed = [
        r for r in result["pricing_recommendations"]
        if r.get("action") in ("markdown",) and r.get("discount_pct", 0) <= 15
    ]
    pending = [
        r for r in result["pricing_recommendations"]
        if r.get("action") in ("clearance_code", "bundle", "increase")
        or (r.get("action") == "markdown" and r.get("discount_pct", 0) > 15)
    ]

    print(
        f"[Supervisor] ✓ Pricing done. "
        f"{len(result['pricing_recommendations'])} decisions: "
        f"{len(auto_executed)} auto-executed, {len(pending)} pending approval, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "pricing_recommendations": result["pricing_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["pricing"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — run_restock_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_restock_agent(state: FashionOSState) -> dict:
    """
    Runs the Restock Agent subgraph.

    Runs AFTER both Inventory + Pricing so:
    - inventory_snapshot is populated (urgency + velocity per SKU)
    - pricing_recommendations is populated (skip clearance SKUs)
    """
    if "restock" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Restock Agent…")

    subgraph_input = {
        "brand_id":                state["brand_id"],
        "brand_name":              state["brand_name"],
        "inventory_snapshot":      state.get("inventory_snapshot", []),
        "pricing_recommendations": state.get("pricing_recommendations", []),
        "restock_candidates":      [],
        "skill_content":           "",
        "raw_analysis":            "",
        "restock_recommendations": [],
        "alerts":                  [],
    }

    result = await restock_graph.ainvoke(subgraph_input)

    print(
        f"[Supervisor] ✓ Restock done. "
        f"{len(result['restock_recommendations'])} orders queued, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "restock_recommendations": result["restock_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["restock"],
    }


# ── Future agent stubs — uncomment + wire as each agent is built ──────────────

# async def run_content_agent(state: FashionOSState) -> dict:
#     if "content" not in state.get("agents_to_run", []):
#         return {}
#     from agents.content.graph import content_graph
#     result = await content_graph.ainvoke({...})
#     return {"content_queue": result["content_queue"], "alerts": result["alerts"],
#             "completed_agents": state.get("completed_agents", []) + ["content"]}

# async def run_marketing_agent(state: FashionOSState) -> dict: ...


# ══════════════════════════════════════════════════════════════════════════════
# NODE 6 — summarize
# ══════════════════════════════════════════════════════════════════════════════

async def summarize(state: FashionOSState) -> dict:
    """
    Generates a concise human-readable summary of the entire agent run.
    Written to state.run_summary — surfaced in the dashboard and daily digest.
    """
    completed = state.get("completed_agents", [])
    snapshots = state.get("inventory_snapshot", [])
    alerts    = state.get("alerts", [])
    pricing   = state.get("pricing_recommendations", [])
    restocks  = state.get("restock_recommendations", [])
    trends    = state.get("trend_signals", [])

    if not completed:
        return {
            "run_summary":  "No agents ran this cycle.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Inventory breakdown ───────────────────────────────────────────────────
    critical_skus = [s for s in snapshots if s.get("urgency") == "critical"]
    high_skus     = [s for s in snapshots if s.get("urgency") == "high"]

    # ── Trend breakdown ───────────────────────────────────────────────────────
    rising_trends    = [t for t in trends if t.get("direction") == "rising"]
    matched_trends   = [t for t in trends if t.get("matched_sku")]
    new_opp_trends   = [t for t in trends if not t.get("matched_sku") and t.get("score", 0) > 0.5]

    # ── Pricing breakdown ─────────────────────────────────────────────────────
    markdowns_auto  = [p for p in pricing if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15]
    pending_pricing = [p for p in pricing if p.get("action") in ("markdown", "clearance_code", "increase", "bundle") and p.get("discount_pct", 0) > 15]

    # ── Restock breakdown ─────────────────────────────────────────────────────
    critical_restocks      = [r for r in restocks if r.get("urgency") == "critical"]
    total_units_to_order   = sum(r.get("recommended_quantity", 0) for r in restocks)

    # ── Alert breakdown ───────────────────────────────────────────────────────
    critical_alerts = [a for a in alerts if a.get("level") == "critical"]
    warning_alerts  = [a for a in alerts if a.get("level") == "warning"]

    run_data = {
        "brand":          state["brand_name"],
        "trigger":        state.get("trigger"),
        "agents_run":     completed,
        # Inventory
        "total_skus":          len(snapshots),
        "critical_skus":       [s["sku"] for s in critical_skus],
        "high_risk_skus":      [s["sku"] for s in high_skus],
        # Trends (new)
        "trend_signals_total": len(trends),
        "rising_trends":       [t["keyword"] for t in rising_trends],
        "catalog_matched":     [t["matched_sku"] for t in matched_trends],
        "new_opportunities":   [t["keyword"] for t in new_opp_trends],
        # Pricing
        "markdowns_auto_executed":    len(markdowns_auto),
        "markdowns_pending_approval": len(pending_pricing),
        "pending_pricing_skus":       [p["sku"] for p in pending_pricing],
        # Restock
        "restock_orders_queued":  len(restocks),
        "critical_restock_skus":  [r["sku"] for r in critical_restocks],
        "total_units_to_order":   total_units_to_order,
        # Alerts
        "critical_alerts": len(critical_alerts),
        "warning_alerts":  len(warning_alerts),
        "supervisor_reasoning": state.get("supervisor_reasoning", ""),
    }

    system_prompt = (
        "You are writing a brief operational summary for a fashion brand owner. "
        "Be direct, specific, and action-oriented. "
        "2–4 sentences maximum. No fluff. "
        "Lead with the most urgent item. "
        "Mention trend signals if present, auto-executed pricing, pending approvals, "
        "and restock orders."
    )
    user_message = (
        f"Write a run summary for this FashionOS agent cycle:\n{run_data}\n\n"
        "Focus on: what's urgent, what the agents actually did, "
        "trending products, and what needs human attention."
    )

    response = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ])

    summary_text = response.content.strip()
    print(f"[Supervisor] Run summary: {summary_text}")

    return {
        "run_summary":  summary_text,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_supervisor_graph() -> StateGraph:
    """
    Assembles the top-level Supervisor graph using FashionOSState.

    Execution order:
      decide_agents → inventory → trend → pricing → restock → summarize

    Adding a new agent = 3 steps:
      1. Import its compiled graph + write run_X_agent() node
      2. add_node() + splice into edge chain before "summarize"
      3. Update decide_agents() routing + summarize() run_data dict
    """
    graph = StateGraph(FashionOSState)

    graph.add_node("decide_agents",       decide_agents)
    graph.add_node("run_inventory_agent", run_inventory_agent)
    graph.add_node("run_trend_agent",     run_trend_agent)
    graph.add_node("run_pricing_agent",   run_pricing_agent)
    graph.add_node("run_restock_agent",   run_restock_agent)
    # graph.add_node("run_content_agent",   run_content_agent)   # TODO
    # graph.add_node("run_marketing_agent", run_marketing_agent)  # TODO
    graph.add_node("summarize",           summarize)

    graph.add_edge(START,                  "decide_agents")
    graph.add_edge("decide_agents",        "run_inventory_agent")
    graph.add_edge("run_inventory_agent",  "run_trend_agent")       # ← Trend BEFORE Pricing
    graph.add_edge("run_trend_agent",      "run_pricing_agent")
    graph.add_edge("run_pricing_agent",    "run_restock_agent")
    # graph.add_edge("run_restock_agent",   "run_content_agent")    # TODO
    graph.add_edge("run_restock_agent",    "summarize")
    graph.add_edge("summarize",            END)

    return graph.compile()


supervisor_graph = build_supervisor_graph()


# ══════════════════════════════════════════════════════════════════════════════
# State factory
# ══════════════════════════════════════════════════════════════════════════════

def make_initial_state(
    brand_id:        str,
    brand_name:      str,
    trigger:         str,
    trigger_payload: dict,
    agents_to_run:   list[str] | None = None,
) -> FashionOSState:
    """
    Builds a properly initialised FashionOSState for a new supervisor run.
    All Annotated[list, operator.add] fields start as empty lists.
    """
    return FashionOSState(
        brand_id   = brand_id,
        brand_name = brand_name,

        trigger         = trigger,
        trigger_payload = trigger_payload,
        run_id          = str(uuid.uuid4()),
        started_at      = datetime.now(timezone.utc).isoformat(),

        products           = [],
        recent_orders      = [],
        sales_velocity     = [],
        inventory_snapshot = [],

        trend_signals            = [],
        pricing_recommendations  = [],
        restock_recommendations  = [],
        marketing_actions        = [],
        content_queue            = [],
        dm_replies               = [],
        alerts                   = [],

        agents_to_run        = agents_to_run or [],
        completed_agents     = [],
        next_agent           = None,
        supervisor_reasoning = "",

        run_summary  = None,
        completed_at = None,
    )