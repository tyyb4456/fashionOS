"""
FashionOS Supervisor Agent
==========================
Central orchestrator. Every agent run passes through here.

Execution order (full daily / manual sweep):
  decide_agents
    → run_inventory_agent    ← products + snapshots propagated to state
    → run_trend_agent        ← trend_signals written BEFORE pricing
    → run_pricing_agent      ← reads trend_signals + inventory_snapshot
    → run_restock_agent      ← reads inventory + pricing
    → run_content_agent      ← reads trend + inventory + pricing
    → run_returns_agent      ← reads inventory_snapshot for return rate calc (NEW)
    → summarize
    → END

Routing table:
  ┌──────────────────────────────┬──────────────────────────────────────────────────────┐
  │ Trigger                      │ Agents                                               │
  ├──────────────────────────────┼──────────────────────────────────────────────────────┤
  │ shopify_webhook  orders/*    │ inventory → pricing → restock                        │
  │ shopify_webhook  refunds/*   │ returns   (NEW — real-time on each refund)           │
  │ shopify_webhook  inventory/* │ inventory                                            │
  │ shopify_webhook  products/*  │ inventory                                            │
  │ scheduled_run    hourly      │ inventory                                            │
  │ scheduled_run    daily       │ inventory→trend→pricing→restock→content→returns      │
  │ manual                       │ same as daily                                        │
  └──────────────────────────────┴──────────────────────────────────────────────────────┘
"""

import os
import uuid
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.content.graph   import content_graph
from agents.inventory.graph import inventory_graph
from agents.pricing.graph   import pricing_graph
from agents.restock.graph   import restock_graph
from agents.returns.graph   import returns_graph
from agents.trend.graph     import trend_graph
from agents.state           import FashionOSState

from langchain.chat_models import init_chat_model


llm = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — decide_agents
# ══════════════════════════════════════════════════════════════════════════════

def decide_agents(state: FashionOSState) -> dict:
    """
    Pure routing — no LLM call.

    Agent registry:
      "inventory" ✓  "trend"   ✓  "pricing" ✓
      "restock"   ✓  "content" ✓  "returns" ✓
      "marketing" ✗  "dm"      ✗
    """
    trigger         = state.get("trigger", "manual")
    trigger_payload = state.get("trigger_payload", {})

    if trigger == "shopify_webhook":
        topic = trigger_payload.get("topic", "")

        if topic.startswith("orders/"):
            agents = ["inventory", "pricing", "restock"]
            reasoning = (
                f"Order webhook ({topic}). "
                "Inventory + pricing + restock. "
                "Trend/Content/Returns skipped — not needed per-order."
            )

        elif topic.startswith("refunds/"):
            # Real-time returns analysis on every refund event
            agents = ["returns"]
            reasoning = (
                f"Refund webhook ({topic}). "
                "Returns Agent analyses the return pattern immediately. "
                "No inventory_snapshot available — return rate uses absolute counts."
            )

        elif topic.startswith("inventory_levels/"):
            agents = ["inventory"]
            reasoning = f"Inventory adjustment ({topic}) — refreshing stock levels only."

        elif topic.startswith("products/"):
            agents = ["inventory"]
            reasoning = f"Product change ({topic}) — refreshing inventory snapshot."

        else:
            agents = ["inventory"]
            reasoning = f"Unknown webhook topic '{topic}' — inventory sweep as safe default."

    elif trigger == "scheduled_run":
        schedule_type = trigger_payload.get("schedule_type", "daily")

        if schedule_type == "hourly":
            agents = ["inventory"]
            reasoning = "Hourly sweep: inventory velocity refresh only."

        elif schedule_type == "daily":
            agents = ["inventory", "trend", "pricing", "restock", "content", "returns"]
            reasoning = (
                "Daily full sweep: all 6 operational agents. "
                "Order: inventory → trend → pricing → restock → content → returns. "
                "inventory_snapshot is in state for returns rate calculation."
            )

        else:
            agents = ["inventory"]
            reasoning = f"Scheduled ({schedule_type}) — defaulting to inventory."

    elif trigger == "manual":
        manual_agents = state.get("agents_to_run", [])
        agents = (
            manual_agents
            if manual_agents
            else ["inventory", "trend", "pricing", "restock", "content", "returns"]
        )
        reasoning = (
            f"Manual trigger. Running: {', '.join(agents)}."
            if manual_agents
            else "Manual — running full pipeline (all 6 agents)."
        )

    else:
        agents = ["inventory"]
        reasoning = f"Unknown trigger '{trigger}' — inventory sweep as safe default."

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
    Runs Inventory Agent. Propagates products → parent state so
    Trend, Content, and Returns agents can use catalog + velocity data
    without redundant MCP calls.
    """
    if "inventory" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Inventory Agent…")

    result = await inventory_graph.ainvoke({
        "brand_id":           state["brand_id"],
        "brand_name":         state["brand_name"],
        "products":           state.get("products", []),
        "sales_velocity":     state.get("sales_velocity", []),
        "skill_content":      "",
        "raw_analysis":       "",
        "inventory_snapshot": [],
        "alerts":             [],
    })

    print(
        f"[Supervisor] ✓ Inventory done. "
        f"{len(result['inventory_snapshot'])} snapshots, "
        f"{len(result['alerts'])} alerts, "
        f"{len(result.get('products', []))} products propagated."
    )

    return {
        "inventory_snapshot": result["inventory_snapshot"],
        "products":           result.get("products", []),
        "alerts":             result["alerts"],
        "completed_agents":   state.get("completed_agents", []) + ["inventory"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_trend_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_trend_agent(state: FashionOSState) -> dict:
    """Runs Trend Agent. Writes trend_signals BEFORE Pricing runs."""
    if "trend" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Trend Agent…")

    result = await trend_graph.ainvoke({
        "brand_id":      state["brand_id"],
        "brand_name":    state["brand_name"],
        "products":      state.get("products", []),
        "social_signals":[],
        "trend_data":    [],
        "skill_content": "",
        "raw_analysis":  "",
        "trend_signals": [],
        "alerts":        [],
    })

    rising  = [s for s in result["trend_signals"] if s.get("direction") == "rising"]
    matched = [s for s in result["trend_signals"] if s.get("matched_sku")]

    print(
        f"[Supervisor] ✓ Trend done. "
        f"{len(result['trend_signals'])} signals "
        f"({len(rising)} rising, {len(matched)} matched), "
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
    """Runs Pricing Agent. Reads trend_signals + inventory_snapshot."""
    if "pricing" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Pricing Agent…")

    result = await pricing_graph.ainvoke({
        "brand_id":                state["brand_id"],
        "brand_name":              state["brand_name"],
        "inventory_snapshot":      state.get("inventory_snapshot", []),
        "trend_signals":           state.get("trend_signals", []),
        "products":                [],
        "sales_velocity":          [],
        "existing_price_rules":    [],
        "skill_content":           "",
        "raw_analysis":            "",
        "pricing_recommendations": [],
        "alerts":                  [],
    })

    print(
        f"[Supervisor] ✓ Pricing done. "
        f"{len(result['pricing_recommendations'])} decisions, "
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
    """Runs Restock Agent. Reads inventory + pricing. All orders pending_approval."""
    if "restock" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Restock Agent…")

    result = await restock_graph.ainvoke({
        "brand_id":                state["brand_id"],
        "brand_name":              state["brand_name"],
        "inventory_snapshot":      state.get("inventory_snapshot", []),
        "pricing_recommendations": state.get("pricing_recommendations", []),
        "restock_candidates":      [],
        "skill_content":           "",
        "raw_analysis":            "",
        "restock_recommendations": [],
        "alerts":                  [],
    })

    print(
        f"[Supervisor] ✓ Restock done. "
        f"{len(result['restock_recommendations'])} orders, "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "restock_recommendations": result["restock_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["restock"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 6 — run_content_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_content_agent(state: FashionOSState) -> dict:
    """Runs Content Agent. Generates Instagram captions + TikTok scripts. No MCP."""
    if "content" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Content Agent…")

    result = await content_graph.ainvoke({
        "brand_id":                state["brand_id"],
        "brand_name":              state["brand_name"],
        "products":                state.get("products", []),
        "trend_signals":           state.get("trend_signals", []),
        "inventory_snapshot":      state.get("inventory_snapshot", []),
        "pricing_recommendations": state.get("pricing_recommendations", []),
        "content_candidates":      [],
        "skill_content":           "",
        "raw_analysis":            "",
        "content_queue":           [],
        "alerts":                  [],
    })

    urgent = [p for p in result["content_queue"] if p.get("is_urgent")]

    print(
        f"[Supervisor] ✓ Content done. "
        f"{len(result['content_queue'])} posts generated "
        f"({len(urgent)} urgent — post today), "
        f"{len(result['alerts'])} alerts."
    )

    return {
        "content_queue":    result["content_queue"],
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["content"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 7 — run_returns_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_returns_agent(state: FashionOSState) -> dict:
    """
    Runs Returns Agent. Calls get_returns via shopify-mcp.
    Uses inventory_snapshot from state (if present) for return rate calculation.
    Runs on refunds/* webhooks (real-time) AND daily sweeps.
    """
    if "returns" not in state.get("agents_to_run", []):
        return {}

    print("[Supervisor] → Dispatching Returns Agent…")

    result = await returns_graph.ainvoke({
        "brand_id":           state["brand_id"],
        "brand_name":         state["brand_name"],
        "inventory_snapshot": state.get("inventory_snapshot", []),  # may be empty on webhook
        "raw_returns":        [],
        "returns_by_sku":     [],
        "skill_content":      "",
        "raw_analysis":       "",
        "alerts":             [],
    })

    critical = [a for a in result["alerts"] if a.get("level") == "critical"]
    warnings = [a for a in result["alerts"] if a.get("level") == "warning"]

    print(
        f"[Supervisor] ✓ Returns done. "
        f"{len(result['alerts'])} alerts "
        f"({len(critical)} critical, {len(warnings)} warning)."
    )

    return {
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["returns"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 8 — summarize
# ══════════════════════════════════════════════════════════════════════════════

async def summarize(state: FashionOSState) -> dict:
    """Generates run summary. Now includes returns stats."""
    completed = state.get("completed_agents", [])

    if not completed:
        return {
            "run_summary":  "No agents ran this cycle.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    snapshots = state.get("inventory_snapshot", [])
    alerts    = state.get("alerts", [])
    pricing   = state.get("pricing_recommendations", [])
    restocks  = state.get("restock_recommendations", [])
    trends    = state.get("trend_signals", [])
    content   = state.get("content_queue", [])

    # Returns alerts (from returns_agent)
    return_alerts = [
        a for a in alerts
        if a.get("agent") == "returns_agent" and a.get("level") in ("critical", "warning")
    ]

    run_data = {
        "brand":      state["brand_name"],
        "trigger":    state.get("trigger"),
        "agents_run": completed,
        # Inventory
        "total_skus":    len(snapshots),
        "critical_skus": [s["sku"] for s in snapshots if s.get("urgency") == "critical"],
        "high_risk_skus":[s["sku"] for s in snapshots if s.get("urgency") == "high"],
        # Trends
        "rising_trends":   [t["keyword"] for t in trends if t.get("direction") == "rising"],
        "catalog_matched": [t["matched_sku"] for t in trends if t.get("matched_sku")],
        # Pricing
        "markdowns_auto":    len([p for p in pricing if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15]),
        "pricing_pending":   len([p for p in pricing if p.get("action") in ("markdown", "clearance_code", "increase", "bundle") and p.get("discount_pct", 0) > 15]),
        # Restock
        "restock_orders":       len(restocks),
        "total_units_to_order": sum(r.get("recommended_quantity", 0) for r in restocks),
        "critical_restock_skus":[r["sku"] for r in restocks if r.get("urgency") == "critical"],
        # Content
        "content_posts_generated": len(content),
        "urgent_posts":            len([p for p in content if p.get("is_urgent")]),
        "urgent_post_skus":        [p["sku"] for p in content if p.get("is_urgent")],
        # Returns (NEW)
        "return_issues_found":     len(return_alerts),
        "critical_return_skus":    [a["sku"] for a in return_alerts if a.get("level") == "critical" and a.get("sku")],
        "warning_return_skus":     [a["sku"] for a in return_alerts if a.get("level") == "warning" and a.get("sku")],
        # Overall alerts
        "critical_alerts": len([a for a in alerts if a.get("level") == "critical"]),
        "warning_alerts":  len([a for a in alerts if a.get("level") == "warning"]),
    }

    response = await llm.ainvoke([
        SystemMessage(content=(
            "You are writing a brief operational summary for a fashion brand owner. "
            "Direct, specific, action-oriented. 2-4 sentences max. No fluff. "
            "Lead with most urgent item. "
            "If return issues found, mention which SKUs and the fix needed. "
            "If content posts ready, mention which to film today."
        )),
        HumanMessage(content=(
            f"Write a run summary for this FashionOS cycle:\n{run_data}"
        )),
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
    graph = StateGraph(FashionOSState)

    graph.add_node("decide_agents",       decide_agents)
    graph.add_node("run_inventory_agent", run_inventory_agent)
    graph.add_node("run_trend_agent",     run_trend_agent)
    graph.add_node("run_pricing_agent",   run_pricing_agent)
    graph.add_node("run_restock_agent",   run_restock_agent)
    graph.add_node("run_content_agent",   run_content_agent)
    graph.add_node("run_returns_agent",   run_returns_agent)
    graph.add_node("summarize",           summarize)

    graph.add_edge(START,                  "decide_agents")
    graph.add_edge("decide_agents",        "run_inventory_agent")
    graph.add_edge("run_inventory_agent",  "run_trend_agent")
    graph.add_edge("run_trend_agent",      "run_pricing_agent")
    graph.add_edge("run_pricing_agent",    "run_restock_agent")
    graph.add_edge("run_restock_agent",    "run_content_agent")
    graph.add_edge("run_content_agent",    "run_returns_agent")
    graph.add_edge("run_returns_agent",    "summarize")
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