"""
FashionOS Supervisor Agent
==========================
The central orchestrator. Every agent run passes through here.

Responsibilities:
  1. Inspect the incoming trigger + payload
  2. Decide which Phase 2 agents to activate (routing)
  3. Execute selected agent subgraphs (sequentially now, parallel later)
  4. Write a human-readable run_summary to state

Graph topology:

    START
      │
      ▼
  decide_agents          ← Reads trigger + payload → sets agents_to_run
      │
      ▼
  run_inventory_agent    ← Inventory subgraph 🗸
      │
      ▼
  run_pricing_agent      ← Pricing subgraph 🗸 (reads inventory_snapshot from state)
      │
      ▼
  [run_trend_agent]      ← TODO: add as agents are built
  [run_restock_agent]
  [run_content_agent]
  [run_marketing_agent]
      │
      ▼
  summarize              ← Writes run_summary + completed_at
      │
      ▼
    END

Routing logic (current — expands as agents are built):
  ┌─────────────────────────────┬──────────────────────────────────────┐
  │ Trigger                     │ Agents activated                     │
  ├─────────────────────────────┼──────────────────────────────────────┤
  │ shopify_webhook (order)     │ inventory → pricing                  │
  │ shopify_webhook (inventory) │ inventory only                       │
  │ scheduled_run (hourly)      │ inventory only                       │
  │ scheduled_run (daily)       │ inventory → pricing                  │
  │ manual                      │ whatever agents_to_run specifies     │
  └─────────────────────────────┴──────────────────────────────────────┘

Usage:
  # Standalone (Celery task / test)
  result = await supervisor_graph.ainvoke(initial_state)

  # The compiled graph is exported as `supervisor_graph` at module level.
  from agents.supervisor import supervisor_graph
"""

import os
import uuid
from datetime import datetime, timezone


from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.inventory.graph import inventory_graph
from agents.pricing.graph import pricing_graph
from agents.state import FashionOSState

from langchain.chat_models import init_chat_model


llm = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — decide_agents
# ══════════════════════════════════════════════════════════════════════════════

def decide_agents(state: FashionOSState) -> dict:
    """
    Pure routing node — no LLM call, just logic.

    Reads state.trigger + state.trigger_payload to determine which agents
    to activate this run. Writes agents_to_run back to state.

    Design: keep this deterministic and fast. Routing should never block on
    an LLM call. The supervisor LLM reasoning (state.supervisor_reasoning)
    is set here as a human-readable explanation of the decision.

    Agent registry (grows as agents are built):
      "inventory"  → agents/inventory/graph.py   🗸 BUILT
      "pricing"    → agents/pricing/graph.py     🗸 BUILT
      "trend"      → agents/trend/graph.py       ✗ TODO
      "restock"    → agents/restock/graph.py     ✗ TODO
      "content"    → agents/content/graph.py     ✗ TODO
      "marketing"  → agents/marketing/graph.py   ✗ TODO
      "dm"         → agents/dm/graph.py          ✗ TODO
      "returns"    → agents/returns/graph.py     ✗ TODO
    """
    trigger         = state.get("trigger", "manual")
    trigger_payload = state.get("trigger_payload", {})

    # ── Routing table ─────────────────────────────────────────────────────────

    if trigger == "shopify_webhook":
        topic = trigger_payload.get("topic", "")

        if topic.startswith("orders/"):
            # A sale happened → velocity changed → re-run pricing on fresh inventory data
            agents = ["inventory", "pricing"]
            reasoning = (
                f"Shopify order webhook ({topic}) received. "
                "Inventory Agent updates velocity + stockout risk. "
                "Pricing Agent then re-evaluates markdowns with fresh data."
            )

        elif topic.startswith("inventory_levels/"):
            # Manual stock adjustment — re-check stockout risk only, no pricing change
            agents = ["inventory"]
            reasoning = (
                f"Inventory level change webhook ({topic}) received. "
                "Running Inventory Agent only to recalculate days-of-stock-remaining."
            )

        elif topic.startswith("products/"):
            # Product created / updated — inventory re-check
            agents = ["inventory"]
            reasoning = (
                f"Product change webhook ({topic}) received. "
                "Refreshing inventory snapshot."
            )

        else:
            agents = ["inventory"]
            reasoning = (
                f"Unknown webhook topic '{topic}'. "
                "Running Inventory Agent as a safe default."
            )

    elif trigger == "scheduled_run":
        schedule_type = trigger_payload.get("schedule_type", "daily")

        if schedule_type == "hourly":
            # Hourly: inventory only — pricing runs daily to avoid thrashing prices
            agents = ["inventory"]
            reasoning = "Hourly scheduled run: inventory velocity refresh only."

        elif schedule_type == "daily":
            # Full daily sweep: inventory first, then pricing with fresh snapshot
            agents = ["inventory", "pricing"]
            reasoning = (
                "Daily scheduled run: full inventory sweep + pricing review. "
                "Trend Agent will be added to this run once built."
            )

        else:
            agents = ["inventory"]
            reasoning = f"Scheduled run (type={schedule_type}): defaulting to inventory sweep."

    elif trigger == "manual":
        manual_agents = state.get("agents_to_run", [])
        agents    = manual_agents if manual_agents else ["inventory", "pricing"]
        reasoning = (
            f"Manual trigger. Running: {', '.join(agents)}."
            if manual_agents
            else "Manual trigger with no explicit agents — running inventory + pricing."
        )

    else:
        agents    = ["inventory"]
        reasoning = f"Unknown trigger '{trigger}'. Defaulting to inventory sweep."

    print(f"[Supervisor] Trigger: {trigger} → Agents: {agents}")
    print(f"[Supervisor] Reasoning: {reasoning}")

    return {
        "agents_to_run":       agents,
        "completed_agents":    [],
        "supervisor_reasoning": reasoning,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — run_inventory_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_inventory_agent(state: FashionOSState) -> dict:
    """
    Calls the Inventory Agent subgraph as a node in the Supervisor graph.

    LangGraph subgraph invocation pattern:
    - We pass in a dict of only the keys the subgraph's state needs.
    - The subgraph runs its own 4-node graph internally.
    - We unpack its output and merge inventory_snapshot + alerts back
      into the parent FashionOSState.

    Why not use add_node("inventory", inventory_graph) directly?
    The direct subgraph embedding works but requires the parent state and
    subgraph state to be identical or perfectly key-mapped. Since we're
    adding a few subgraph-internal keys (skill_content, raw_analysis) that
    don't exist in FashionOSState, an explicit call is cleaner and avoids
    LangGraph TypedDict validation errors during the build phase.
    """
    if "inventory" not in state.get("agents_to_run", []):
        # Supervisor decided this agent isn't needed this run
        return {}

    print("[Supervisor] → Dispatching Inventory Agent…")

    # Prepare subgraph input (keys that InventoryAgentState needs)
    subgraph_input = {
        "brand_id":           state["brand_id"],
        "brand_name":         state["brand_name"],
        "products":           state.get("products", []),
        "sales_velocity":     state.get("sales_velocity", []),
        "skill_content":      "",    # Node 2 of subgraph loads this
        "raw_analysis":       "",    # Node 3 of subgraph fills this
        "inventory_snapshot": [],
        "alerts":             [],
    }

    result = await inventory_graph.ainvoke(subgraph_input)

    print(
        f"[Supervisor] 🗸 Inventory Agent done. "
        f"{len(result['inventory_snapshot'])} snapshots, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "inventory_snapshot": result["inventory_snapshot"],
        "alerts":             result["alerts"],
        "completed_agents":   state.get("completed_agents", []) + ["inventory"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_pricing_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_pricing_agent(state: FashionOSState) -> dict:
    """
    Calls the Pricing Agent subgraph.

    Runs AFTER run_inventory_agent so state.inventory_snapshot is already
    populated. The Pricing Agent reads it directly — no extra Shopify call
    needed for inventory data. This is the composability benefit of shared state.

    Passes trend_signals through (empty list until Trend Agent is built —
    Pricing Agent handles the empty case gracefully).
    """
    if "pricing" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Pricing Agent…")

    subgraph_input = {
        "brand_id":               state["brand_id"],
        "brand_name":             state["brand_name"],
        # Key handoff: inventory_snapshot set by Inventory Agent above
        "inventory_snapshot":     state.get("inventory_snapshot", []),
        "trend_signals":          state.get("trend_signals", []),
        # Node 1 of pricing subgraph fetches these fresh from shopify-mcp
        "products":               [],
        "sales_velocity":         [],
        "existing_price_rules":   [],
        # Scratch fields
        "skill_content":          "",
        "raw_analysis":           "",
        # Output accumulators
        "pricing_recommendations": [],
        "alerts":                  [],
    }

    result = await pricing_graph.ainvoke(subgraph_input)

    auto_executed = [
        r for r in result["pricing_recommendations"]
        if r.get("action") in ("markdown", "increase")
    ]
    pending = [
        r for r in result["pricing_recommendations"]
        if r.get("action") in ("clearance_code", "bundle")
    ]

    print(
        f"[Supervisor] 🗸 Pricing Agent done. "
        f"{len(result['pricing_recommendations'])} decisions: "
        f"{len(auto_executed)} executed, {len(pending)} pending approval, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "pricing_recommendations": result["pricing_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["pricing"],
    }


# ── Future agent stubs — uncomment + implement as each agent is built ─────────

# async def run_trend_agent(state: FashionOSState) -> dict:
#     if "trend" not in state.get("agents_to_run", []):
#         return {}
#     from agents.trend.graph import trend_graph
#     result = await trend_graph.ainvoke({
#         "brand_id": state["brand_id"], "brand_name": state["brand_name"],
#         "products": state.get("products", []),
#         "trend_signals": [], "alerts": [],
#         "skill_content": "", "raw_analysis": "",
#     })
#     return {"trend_signals": result["trend_signals"], "alerts": result["alerts"],
#             "completed_agents": state.get("completed_agents", []) + ["trend"]}

# async def run_restock_agent(state: FashionOSState) -> dict: ...
# async def run_content_agent(state: FashionOSState) -> dict: ...
# async def run_marketing_agent(state: FashionOSState) -> dict: ...


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — summarize
# ══════════════════════════════════════════════════════════════════════════════

async def summarize(state: FashionOSState) -> dict:
    """
    Generates a concise human-readable summary of the entire agent run.
    Written to state.run_summary — surfaced in the dashboard and daily digest.

    Uses a minimal LLM call (max_tokens=512) — just enough to produce a
    natural language paragraph from the structured data. Not an agent loop.
    """
    completed = state.get("completed_agents", [])
    snapshots = state.get("inventory_snapshot", [])
    alerts    = state.get("alerts", [])
    pricing   = state.get("pricing_recommendations", [])

    # Build a compact run report for the LLM
    critical_skus = [s for s in snapshots if s.get("urgency") == "critical"]
    high_skus     = [s for s in snapshots if s.get("urgency") == "high"]
    warnings      = [a for a in alerts if a.get("level") == "warning"]
    criticals     = [a for a in alerts if a.get("level") == "critical"]

    # Pricing breakdown
    markdowns_executed = [
        p for p in pricing
        if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15
    ]
    pending_approval = [
        p for p in pricing
        if p.get("action") in ("markdown", "clearance_code", "increase", "bundle")
        and p.get("discount_pct", 0) > 15
    ]

    run_data = {
        "brand":           state["brand_name"],
        "trigger":         state.get("trigger"),
        "agents_run":      completed,
        # Inventory
        "total_skus":      len(snapshots),
        "critical_skus":   [s["sku"] for s in critical_skus],
        "high_risk_skus":  [s["sku"] for s in high_skus],
        # Pricing
        "markdowns_auto_executed": len(markdowns_executed),
        "markdowns_pending_approval": len(pending_approval),
        "pending_skus": [p["sku"] for p in pending_approval],
        # Alerts
        "total_alerts":    len(alerts),
        "critical_alerts": len(criticals),
        "warning_alerts":  len(warnings),
        "supervisor_reasoning": state.get("supervisor_reasoning", ""),
    }

    if not completed:
        return {
            "run_summary":  "No agents ran this cycle.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    system_prompt = (
        "You are writing a brief operational summary for a fashion brand owner. "
        "Be direct, specific, and action-oriented. "
        "Write 2–4 sentences maximum. No fluff. "
        "Always lead with the most urgent item. "
        "Mention any auto-executed pricing changes and any pending approvals needing attention."
    )
    user_message = (
        f"Write a run summary for this FashionOS agent cycle:\n"
        f"{run_data}\n\n"
        "Focus on: what was checked, what's urgent, what pricing actions ran, "
        "what needs human attention today."
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

    Current execution order:
      decide_agents → run_inventory_agent → run_pricing_agent → summarize

    Adding a new agent = 3 steps:
      1. Import its compiled graph at top of file
      2. Write an async run_X_agent() node (see stubs above)
      3. add_node() + splice into the edge chain before "summarize"
    """
    graph = StateGraph(FashionOSState)

    graph.add_node("decide_agents",       decide_agents)
    graph.add_node("run_inventory_agent", run_inventory_agent)
    graph.add_node("run_pricing_agent",   run_pricing_agent)
    # graph.add_node("run_trend_agent",    run_trend_agent)     # TODO
    # graph.add_node("run_restock_agent",  run_restock_agent)   # TODO
    # graph.add_node("run_content_agent",  run_content_agent)   # TODO
    # graph.add_node("run_marketing_agent",run_marketing_agent) # TODO
    graph.add_node("summarize",           summarize)

    graph.add_edge(START,                 "decide_agents")
    graph.add_edge("decide_agents",       "run_inventory_agent")
    graph.add_edge("run_inventory_agent", "run_pricing_agent")
    # graph.add_edge("run_pricing_agent",  "run_trend_agent")   # TODO
    # graph.add_edge("run_trend_agent",    "run_restock_agent") # TODO
    graph.add_edge("run_pricing_agent",   "summarize")
    graph.add_edge("summarize",           END)

    return graph.compile()


supervisor_graph = build_supervisor_graph()


# ══════════════════════════════════════════════════════════════════════════════
# State factory — builds a clean initial FashionOSState for a new run
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

    All Annotated[list, operator.add] fields must start as empty lists —
    LangGraph merges into them, so None would crash on first append.

    Usage in Celery task:
        state = make_initial_state(
            brand_id="brand-001",
            brand_name="MyBrand",
            trigger="shopify_webhook",
            trigger_payload={"topic": "orders/paid", ...},
        )
        result = await supervisor_graph.ainvoke(state)
    """
    return FashionOSState(
        # Identity
        brand_id   = brand_id,
        brand_name = brand_name,

        # Trigger
        trigger         = trigger,
        trigger_payload = trigger_payload,
        run_id          = str(uuid.uuid4()),
        started_at      = datetime.now(timezone.utc).isoformat(),

        # Live data (populated by agents on demand via MCP)
        products           = [],
        recent_orders      = [],
        sales_velocity     = [],
        inventory_snapshot = [],

        # Agent outputs — all empty lists, operator.add merges into these
        trend_signals            = [],
        pricing_recommendations  = [],
        restock_recommendations  = [],
        marketing_actions        = [],
        content_queue            = [],
        dm_replies               = [],
        alerts                   = [],

        # Supervisor routing
        agents_to_run        = agents_to_run or [],
        completed_agents     = [],
        next_agent           = None,
        supervisor_reasoning = "",

        # Final
        run_summary  = None,
        completed_at = None,
    )