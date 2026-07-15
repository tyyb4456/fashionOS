"""
FashionOS Supervisor Agent
==========================
Session 8: send_notifications node added after summarize.
Sends WhatsApp critical alerts + daily email digest via notify-mcp.

Execution order (full daily / manual sweep):
  decide_agents → inventory → trend → pricing → restock → content
    → returns → marketing → dm → summarize → send_notifications → END

send_notifications runs after summarize on: daily + manual triggers.
It is skipped on: hourly, dm-check, order webhooks (no digest needed).
If notify-mcp is unreachable the pipeline completes silently — notifications
are never blocking.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph

from agents.content.graph   import content_graph
from agents.dm.graph        import dm_graph
from agents.inventory.graph import inventory_graph
from agents.marketing.graph import marketing_graph
from agents.pricing.graph   import pricing_graph
from agents.restock.graph   import restock_graph
from agents.returns.graph   import returns_graph
from agents.trend.graph     import trend_graph
from agents.state           import FashionOSState

from langchain.chat_models import init_chat_model

llm = init_chat_model("google_genai:gemini-2.5-flash-lite")

NOTIFY_MCP_URL = os.getenv("NOTIFY_MCP_URL", "http://localhost:8005/mcp")


# ── Helper ─────────────────────────────────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
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
# NODE 1 — decide_agents
# ══════════════════════════════════════════════════════════════════════════════

def decide_agents(state: FashionOSState) -> dict:
    trigger         = state.get("trigger", "manual")
    trigger_payload = state.get("trigger_payload", {})

    if trigger == "shopify_webhook":
        topic = trigger_payload.get("topic", "")
        if topic.startswith("orders/"):
            agents    = ["inventory", "pricing", "restock"]
            reasoning = f"Order webhook ({topic}). Inventory + pricing + restock."
        elif topic.startswith("refunds/"):
            agents    = ["returns"]
            reasoning = f"Refund webhook ({topic}). Returns Agent immediately."
        elif topic.startswith("inventory_levels/"):
            agents    = ["inventory"]
            reasoning = f"Inventory adjustment ({topic})."
        elif topic.startswith("products/"):
            agents    = ["inventory"]
            reasoning = f"Product change ({topic})."
        else:
            agents    = ["inventory"]
            reasoning = f"Unknown webhook topic '{topic}' — inventory default."

    elif trigger == "scheduled_run":
        schedule_type = trigger_payload.get("schedule_type", "daily")
        if schedule_type == "hourly":
            agents    = ["inventory"]
            reasoning = "Hourly inventory sweep."
        elif schedule_type == "daily":
            agents    = ["inventory", "trend", "pricing", "restock", "content", "returns", "marketing", "dm"]
            reasoning = "Daily full sweep: all 8 agents + notifications."
        elif schedule_type == "dm":
            agents    = ["dm"]
            reasoning = "DM polling sweep."
        else:
            agents    = ["inventory"]
            reasoning = f"Scheduled ({schedule_type}) — inventory default."

    elif trigger == "manual":
        manual_agents = state.get("agents_to_run", [])
        agents = manual_agents or ["inventory", "trend", "pricing", "restock", "content", "returns", "marketing", "dm"]
        reasoning = f"Manual. Running: {', '.join(agents)}."

    else:
        agents    = ["inventory"]
        reasoning = f"Unknown trigger '{trigger}' — inventory default."

    print(f"[Supervisor] Trigger: {trigger} → Agents: {agents}")
    return {
        "agents_to_run":        agents,
        "completed_agents":     [],
        "supervisor_reasoning": reasoning,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODES 2-9 — agent runners (unchanged from session 7)
# ══════════════════════════════════════════════════════════════════════════════

async def run_inventory_agent(state: FashionOSState) -> dict:
    if "inventory" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Inventory Agent…")
    result = await inventory_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "products": state.get("products", []), "sales_velocity": [],
        "skill_content": "", "raw_analysis": "", "inventory_snapshot": [], "alerts": [],
    })
    print(f"[Supervisor] ✓ Inventory: {len(result['inventory_snapshot'])} snapshots, {len(result['alerts'])} alerts.")
    return {
        "inventory_snapshot": result["inventory_snapshot"],
        "products":           result.get("products", []),
        "alerts":             result["alerts"],
        "completed_agents":   state.get("completed_agents", []) + ["inventory"],
        "sales_velocity":     result["sales_velocity"]
    }


async def run_trend_agent(state: FashionOSState) -> dict:
    if "trend" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Trend Agent…")
    result = await trend_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "products": state.get("products", []), "social_signals": [], "trend_data": [],
        "skill_content": "", "trend_history": [], "raw_findings": "", "agent_error": None,
        "computed_signals": [], "computed_alerts": [], "trend_signals": [], "alerts": [],
    })
    print(f"[Supervisor] ✓ Trend: {len(result['trend_signals'])} signals, {len(result['alerts'])} alerts.")
    return {
        "trend_signals":    result["trend_signals"],
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["trend"],
    }


async def run_pricing_agent(state: FashionOSState) -> dict:
    if "pricing" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Pricing Agent…")
    result = await pricing_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "inventory_snapshot": state.get("inventory_snapshot", []),
        "trend_signals": state.get("trend_signals", []),
        "products": [], "sales_velocity": [], "existing_price_rules": [],
        "computed_plan": [], "raw_copy": "",
        "pricing_recommendations": [], "alerts": [],
    })
    print(f"[Supervisor] ✓ Pricing: {len(result['pricing_recommendations'])} decisions, {len(result['alerts'])} alerts.")
    return {
        "pricing_recommendations": result["pricing_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["pricing"],
    }


async def run_restock_agent(state: FashionOSState) -> dict:
    if "restock" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Restock Agent…")
    result = await restock_graph.ainvoke({
            "brand_id": state["brand_id"], "brand_name": state["brand_name"],
            "inventory_snapshot": state.get("inventory_snapshot", []),
            "pricing_recommendations": state.get("pricing_recommendations", []),
            "restock_candidates": [], "computed_plan": [], "raw_copy": "",
            "restock_recommendations": [], "alerts": [],
        })
    print(f"[Supervisor] ✓ Restock: {len(result['restock_recommendations'])} orders, {len(result['alerts'])} alerts.")
    return {
        "restock_recommendations": result["restock_recommendations"],
        "alerts":                  result["alerts"],
        "completed_agents":        state.get("completed_agents", []) + ["restock"],
    }


async def run_content_agent(state: FashionOSState) -> dict:
    if "content" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Content Agent…")
    result = await content_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "products": state.get("products", []),
        "trend_signals": state.get("trend_signals", []),
        "inventory_snapshot": state.get("inventory_snapshot", []),
        "pricing_recommendations": state.get("pricing_recommendations", []),
        "content_candidates": [], "computed_plan": [], "raw_copy": "",
        "content_queue": [], "alerts": [],
    })
    urgent = [p for p in result["content_queue"] if p.get("is_urgent")]
    print(f"[Supervisor] ✓ Content: {len(result['content_queue'])} posts ({len(urgent)} urgent).")
    return {
        "content_queue":    result["content_queue"],
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["content"],
    }


async def run_returns_agent(state: FashionOSState) -> dict:
    if "returns" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Returns Agent…")
    result = await returns_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "inventory_snapshot": state.get("inventory_snapshot", []),
        "raw_returns": [], "returns_by_sku": [], "raw_classifications": "",
        "computed_plan": [], "raw_copy": "",
        "alerts": [], "return_insights": [],
    })
    print(f"[Supervisor] ✓ Returns: {len(result['alerts'])} alerts, {len(result.get('return_insights', []))} insights.")
    return {
        "alerts":           result["alerts"],
        "return_insights":  result.get("return_insights", []),
        "completed_agents": state.get("completed_agents", []) + ["returns"],
    }


async def run_marketing_agent(state: FashionOSState) -> dict:
    if "marketing" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → Marketing Agent…")
    result = await marketing_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "inventory_snapshot": state.get("inventory_snapshot", []),
        "trend_signals": state.get("trend_signals", []),
        "pricing_recommendations": state.get("pricing_recommendations", []),
        "campaigns": [], "computed_plan": [], "raw_copy": "",
        "marketing_actions": [], "alerts": [],
    })
    paused = [a for a in result["marketing_actions"] if a.get("action") == "pause"]
    print(f"[Supervisor] ✓ Marketing: {len(result['marketing_actions'])} decisions ({len(paused)} paused).")
    return {
        "marketing_actions": result["marketing_actions"],
        "alerts":            result["alerts"],
        "completed_agents":  state.get("completed_agents", []) + ["marketing"],
    }


async def run_dm_agent(state: FashionOSState) -> dict:
    if "dm" not in state.get("agents_to_run", []):
        return {}
    print("[Supervisor] → DM Agent…")
    result = await dm_graph.ainvoke({
        "brand_id": state["brand_id"], "brand_name": state["brand_name"],
        "inventory_snapshot": state.get("inventory_snapshot", []),
        "raw_dms": [], "raw_classifications": "", "computed_gating": [], "raw_copy": "",
        "dm_replies": [], "alerts": [],
    })
    auto_sent = sum(1 for r in result["dm_replies"] if r.get("auto_sent"))
    flagged   = sum(1 for r in result["dm_replies"] if r.get("flag_for_human"))
    print(f"[Supervisor] ✓ DM: {auto_sent} auto-replied, {flagged} flagged.")
    return {
        "dm_replies":       result["dm_replies"],
        "alerts":           result["alerts"],
        "completed_agents": state.get("completed_agents", []) + ["dm"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 10 — summarize
# ══════════════════════════════════════════════════════════════════════════════

async def summarize(state: FashionOSState) -> dict:
    completed = state.get("completed_agents", [])
    if not completed:
        return {"run_summary": "No agents ran this cycle.", "completed_at": datetime.now(timezone.utc).isoformat()}

    alerts    = state.get("alerts", [])
    pricing   = state.get("pricing_recommendations", [])
    restocks  = state.get("restock_recommendations", [])
    trends    = state.get("trend_signals", [])
    content   = state.get("content_queue", [])
    marketing = state.get("marketing_actions", [])
    dm_replies= state.get("dm_replies", [])

    run_data = {
        "brand":      state["brand_name"],
        "trigger":    state.get("trigger"),
        "agents_run": completed,
        "critical_skus":         [s["sku"] for s in state.get("inventory_snapshot", []) if s.get("urgency") == "critical"],
        "rising_trends":         [t["keyword"] for t in trends if t.get("direction") == "rising"],
        "markdowns_auto":        len([p for p in pricing if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15]),
        "pricing_pending":       len([p for p in pricing if p.get("action") in ("markdown", "clearance_code") and p.get("discount_pct", 0) > 15]),
        "restock_orders":        len(restocks),
        "critical_restock_skus": [r["sku"] for r in restocks if r.get("urgency") == "critical"],
        "urgent_content_skus":   [p["sku"] for p in content if p.get("is_urgent")],
        "campaigns_paused":      [m["sku"] for m in marketing if m.get("action") == "pause" and m.get("sku")],
        "budget_increases_pending": len([m for m in marketing if m.get("action") == "increase_budget"]),
        "dm_auto_replied":       sum(1 for r in dm_replies if r.get("auto_sent")),
        "dm_high_flagged":       [r.get("username") for r in dm_replies if r.get("flag_for_human") and r.get("category") in ("bulk_inquiry", "complaint")],
        "critical_alerts":       len([a for a in alerts if a.get("level") == "critical"]),
        "warning_alerts":        len([a for a in alerts if a.get("level") == "warning"]),
    }

    response = await llm.ainvoke([
        SystemMessage(content=(
            "Write a 2-4 sentence operational summary for a fashion brand owner. "
            "Direct, specific, action-oriented. Lead with the most urgent item. "
            "Mention critical stockouts, pending approvals, DM flags, content ready to film."
        )),
        HumanMessage(content=f"FashionOS run summary:\n{run_data}"),
    ])

    summary_text = response.content.strip()
    print(f"[Supervisor] Summary: {summary_text}")
    return {
        "run_summary":  summary_text,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 11 — send_notifications  (NEW session 8)
# ══════════════════════════════════════════════════════════════════════════════

async def send_notifications(state: FashionOSState) -> dict:
    """
    Sends WhatsApp + email notifications via notify-mcp.
    Runs after summarize on daily + manual triggers only.

    WhatsApp alerts for:
      - Critical alerts (stockouts, quality issues)
      - High-priority DM flags (bulk_inquiry, complaint)

    Email digest for:
      - Daily sweep only (not per-order or dm-check)

    Non-blocking: if notify-mcp is unreachable, logs and continues silently.
    """
    trigger       = state.get("trigger", "manual")
    schedule_type = state.get("trigger_payload", {}).get("schedule_type", "")
    brand_id      = state.get("brand_id", "unknown_brand")

    # Skip notifications for hourly and dm-check sweeps
    if trigger == "scheduled_run" and schedule_type in ("hourly", "dm"):
        return {}

    # Skip if nothing ran
    if not state.get("completed_agents"):
        return {}

    # ── Connect to notify-mcp ──────────────────────────────────────────────────
    try:
        client   = MultiServerMCPClient(
            {"notify": {"url": NOTIFY_MCP_URL, "transport": "streamable_http"}}
        )
        tools    = await client.get_tools()
        tool_map = {t.name: t for t in tools}
    except Exception as exc:
        print(f"[Supervisor] notify-mcp unreachable — skipping notifications: {exc}")
        return {}

    alerts    = state.get("alerts", [])
    dm_replies = state.get("dm_replies", [])

    critical      = [a for a in alerts if a.get("level") == "critical"]
    dm_high_flags = [r for r in dm_replies if r.get("flag_for_human") and r.get("category") in ("bulk_inquiry", "complaint")]

    # ── 1. Critical alerts → WhatsApp ─────────────────────────────────────────
    if critical and "send_critical_alert" in tool_map:
        # Group into one message (max 5 alerts per WhatsApp)
        msg = f"⚠️ {len(critical)} critical alert(s) this run:\n\n"
        for alert in critical[:5]:
            sku_tag = f"[{alert['sku']}] " if alert.get("sku") else ""
            msg += f"• {sku_tag}{alert['message'][:120]}\n"

        try:
            raw = await tool_map["send_critical_alert"].ainvoke({
                "brand_id":   brand_id,
                "alert_body": msg,
                "sku":        critical[0].get("sku"),
            })
            result = _parse_mcp_result(raw)
            if isinstance(result, dict) and result.get("success"):
                print(f"[Supervisor] ✓ Critical alert WhatsApp sent ({len(critical)} alerts).")
            else:
                print(f"[Supervisor] ✗ Critical alert WhatsApp failed: {result}")
        except Exception as exc:
            print(f"[Supervisor] ✗ send_critical_alert error: {exc}")

    # ── 2. High-priority DM flags → WhatsApp ──────────────────────────────────
    if dm_high_flags and "send_whatsapp_message" in tool_map:
        for dm in dm_high_flags[:3]:
            category = dm.get("category", "").replace("_", " ").title()
            msg = (
                f"📩 *{category}* from @{dm.get('username', 'customer')}\n\n"
                f"{(dm.get('original_message') or '')[:200]}\n\n"
                f"Reply via Instagram DMs."
            )
            try:
                raw = await tool_map["send_critical_alert"].ainvoke({
                    "brand_id":   brand_id,
                    "alert_body": msg,
                    "sku":        None,
                })
                result = _parse_mcp_result(raw)
                if isinstance(result, dict) and result.get("success"):
                    print(f"[Supervisor] ✓ DM flag WhatsApp sent: @{dm.get('username')} [{dm.get('category')}]")
                else:
                    print(f"[Supervisor] ✗ DM flag WhatsApp failed: {result}")
            except Exception as exc:
                print(f"[Supervisor] ✗ DM flag WhatsApp error: {exc}")

    # ── 3. Daily email digest ─────────────────────────────────────────────────
    is_daily = (trigger == "manual") or (trigger == "scheduled_run" and schedule_type == "daily")
    if is_daily and "send_daily_digest" in tool_map:
        pricing   = state.get("pricing_recommendations", [])
        restocks  = state.get("restock_recommendations", [])
        content   = state.get("content_queue", [])
        marketing = state.get("marketing_actions", [])
        trends    = state.get("trend_signals", [])

        # Build highlights + pending lists
        highlights: list[str] = []
        pending:    list[str] = []

        rising = [t["keyword"] for t in trends if t.get("direction") == "rising"]
        if rising:
            highlights.append(f"Rising trends: {', '.join(rising[:3])}")

        auto_priced = [p for p in pricing if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15]
        if auto_priced:
            highlights.append(f"{len(auto_priced)} markdowns auto-applied")

        dm_auto = sum(1 for r in dm_replies if r.get("auto_sent"))
        if dm_auto:
            highlights.append(f"{dm_auto} customer DMs auto-replied")

        urgent_content = [p for p in content if p.get("is_urgent")]
        if urgent_content:
            pending.append(f"Film + post today: {', '.join(p['sku'] for p in urgent_content[:3])}")

        pricing_pending = [p for p in pricing if p.get("action") in ("markdown", "clearance_code") and p.get("discount_pct", 0) > 15]
        if pricing_pending:
            pending.append(f"Approve {len(pricing_pending)} pricing decisions in dashboard")

        if restocks:
            critical_restock = [r for r in restocks if r.get("urgency") == "critical"]
            if critical_restock:
                pending.append(f"URGENT: Approve restock for {', '.join(r['sku'] for r in critical_restock[:3])}")
            else:
                pending.append(f"Review {len(restocks)} restock recommendation(s)")

        budget_increases = [m for m in marketing if m.get("action") == "increase_budget"]
        if budget_increases:
            pending.append(f"Approve {len(budget_increases)} ad budget increase(s)")

        if dm_high_flags:
            dm_handles = ", ".join("@" + (r.get("username") or "?") for r in dm_high_flags[:3])
            pending.append(f"Reply to {len(dm_high_flags)} flagged DM(s): {dm_handles}")

        warning_count = len([a for a in alerts if a.get("level") == "warning"])

        try:
            raw = await tool_map["send_daily_digest"].ainvoke({
                "brand_id":        brand_id, 
                "run_summary":     state.get("run_summary", "Run completed."),
                "critical_count":  len(critical),
                "warning_count":   warning_count,
                "agents_run":      state.get("completed_agents", []),
                "highlights":      highlights or ["Pipeline ran successfully."],
                "pending_actions": pending or ["Nothing pending — all good!"],
            })
            result = _parse_mcp_result(raw)
            if isinstance(result, dict) and result.get("success"):
                print(f"[Supervisor] ✓ Daily digest email sent.")
            else:
                print(f"[Supervisor] ✗ Daily digest failed: {result}")
        except Exception as exc:
            print(f"[Supervisor] ✗ send_daily_digest error: {exc}")

    return {}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_supervisor_graph() -> StateGraph:
    graph = StateGraph(FashionOSState)

    graph.add_node("decide_agents",        decide_agents)
    graph.add_node("run_inventory_agent",  run_inventory_agent)
    graph.add_node("run_trend_agent",      run_trend_agent)
    graph.add_node("run_pricing_agent",    run_pricing_agent)
    graph.add_node("run_restock_agent",    run_restock_agent)
    graph.add_node("run_content_agent",    run_content_agent)
    graph.add_node("run_returns_agent",    run_returns_agent)
    graph.add_node("run_marketing_agent",  run_marketing_agent)
    graph.add_node("run_dm_agent",         run_dm_agent)
    graph.add_node("summarize",            summarize)
    graph.add_node("send_notifications",   send_notifications)   # NEW session 8

    graph.add_edge(START,                  "decide_agents")
    graph.add_edge("decide_agents",        "run_inventory_agent")
    graph.add_edge("run_inventory_agent",  "run_trend_agent")
    graph.add_edge("run_trend_agent",      "run_pricing_agent")
    graph.add_edge("run_pricing_agent",    "run_restock_agent")
    graph.add_edge("run_restock_agent",    "run_content_agent")
    graph.add_edge("run_content_agent",    "run_returns_agent")
    graph.add_edge("run_returns_agent",    "run_marketing_agent")
    graph.add_edge("run_marketing_agent",  "run_dm_agent")
    graph.add_edge("run_dm_agent",         "summarize")
    graph.add_edge("summarize",            "send_notifications")  # NEW session 8
    graph.add_edge("send_notifications",   END)                   # NEW session 8

    return graph.compile()


supervisor_graph = build_supervisor_graph()


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
        return_insights          = [],
        agents_to_run        = agents_to_run or [],
        completed_agents     = [],
        next_agent           = None,
        supervisor_reasoning = "",
        run_summary  = None,
        completed_at = None,
    )