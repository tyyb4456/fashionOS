"""
DM Agent — FashionOS Phase 2 Operations
========================================
Monitors Instagram DMs, classifies incoming messages, auto-replies to common
questions (size, availability, order status), and flags high-value or sensitive
conversations for human review.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  fetch_dm_data        ← Node 1: social-mcp → get_instagram_dms(limit=30).
      │                           Filters to needs_reply=True only.
      │                           Enriches with inventory context from state
      │                           (availability questions get live stock data).
      ▼
  load_domain_skill    ← Node 2: load_skill("fashion_dm")
      │                           Category rules, reply templates, brand voice.
      ▼
  run_claude_analysis  ← Node 3: Structured LLM call.
      │                           Classifies each DM into 8 categories.
      │                           Drafts brand-voice replies for auto-send categories.
      │                           Uses inventory_snapshot for availability answers.
      ▼
  send_dm_replies      ← Node 4: send_instagram_dm() for each auto_send=True.
      │                           Raises AgentAlert for flagged DMs (human review).
      │                           Writes dm_replies + alerts to state.
      ▼
    END

Categories:
  AUTO-REPLY:     size_question, availability, order_status, general_inquiry
  FLAG FOR HUMAN: bulk_inquiry (high), complaint (high), influencer (normal)
  SKIP:           spam

Auto-reply decisions are made by Claude. The rules are in the fashion_dm skill.
Node 4 only executes — it trusts Claude's auto_send flag.

Availability answers are powered by inventory_snapshot already in state from
the Inventory Agent. No extra MCP call needed.

Trigger:
  - Scheduled every 30 minutes via Celery beat (run_scheduled_dm task)
  - Daily full sweep (alongside other agents)
  - NOT on order webhooks (irrelevant)

Standalone test:
  python -m agents.dm.graph
  (requires social-mcp on :8002 with INSTAGRAM creds set)
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
from agents.state import AgentAlert, InventorySnapshot

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SOCIAL_MCP_URL = os.getenv("SOCIAL_MCP_URL", "http://localhost:8002/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

DM_FETCH_LIMIT = int(os.getenv("DM_FETCH_LIMIT", "30"))


from response_schemas.dm_model import DmDecision, DmAnalysis


# ── Subgraph state ─────────────────────────────────────────────────────────────

class DmAgentState(TypedDict):
    # From parent state
    brand_id:   str
    brand_name: str

    # From Inventory Agent — for availability answers (no extra MCP needed)
    inventory_snapshot: list[InventorySnapshot]

    # Node 1 output (internal scratch)
    raw_dms: list[dict]   # Only needs_reply=True DMs

    # Internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → operator.add merges safely with other agents
    dm_replies: Annotated[list[dict], operator.add]
    alerts:     Annotated[list[AgentAlert], operator.add]


# ── Helper ─────────────────────────────────────────────────────────────────────

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
# NODE 1 — fetch_dm_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_dm_data(state: DmAgentState) -> dict:
    """
    Fetches Instagram DMs from social-mcp. Filters to needs_reply=True only.

    Also builds a compact inventory index so Node 3 can answer availability
    questions accurately without an extra MCP call.
    """
    client   = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    raw_dms: list[dict] = []

    if "get_instagram_dms" not in tool_map:
        print("[DM] WARNING: get_instagram_dms not in tool_map — rebuild social-mcp image")
        return {"raw_dms": []}

    try:
        raw = await tool_map["get_instagram_dms"].ainvoke({"limit": DM_FETCH_LIMIT, "brand_id": state["brand_id"]})
        result = _parse_mcp_result(raw)

        if isinstance(result, list) and result and "error" in result[0]:
            print(f"[DM] social-mcp error: {result[0]['error']}")
            return {"raw_dms": []}

        all_dms = result if isinstance(result, list) else []
    except Exception as exc:
        print(f"[DM] get_instagram_dms failed: {exc}")
        return {"raw_dms": []}

    # Filter to only conversations that need a reply
    raw_dms = [dm for dm in all_dms if dm.get("needs_reply", False)]

    print(
        f"[DM] Fetched {len(all_dms)} conversations, "
        f"{len(raw_dms)} need replies."
    )

    return {"raw_dms": raw_dms}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: DmAgentState) -> dict:
    skill = load_skill("fashion_dm")
    print("[DM] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_claude_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_claude_analysis(state: DmAgentState) -> dict:
    """
    Classifies each DM and drafts replies for auto-send categories.

    Availability questions get answered using inventory_snapshot from state —
    Claude gets a compact product+stock table so replies are accurate.
    """
    raw_dms = state.get("raw_dms", [])

    if not raw_dms:
        print("[DM] No DMs to process.")
        empty = DmAnalysis(
            decisions=[], auto_send_count=0, flagged_count=0,
            summary="No unread DMs this cycle.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    # ── Build compact inventory for availability answers ────────────────────
    inventory_compact = [
        {
            "sku":           s.get("sku", ""),
            "product_title": s.get("product_title", ""),
            "variant_title": s.get("variant_title", ""),
            "current_stock": s.get("current_stock", 0),
            "urgency":       s.get("urgency", ""),
        }
        for s in state.get("inventory_snapshot", [])
        if s.get("current_stock", 0) > 0
    ][:30]  # Cap at 30 SKUs for token efficiency

    system_prompt = f"""You are the DM Agent for {state['brand_name']}, \
a Pakistani Instagram fashion brand. You manage customer DMs around the clock.

{state['skill_content']}

## Current inventory (for availability questions)
Use this ONLY when a customer asks about a specific product's availability.
Cross-reference product names/descriptions from the DM text with this table.
If no match found, reply that you'll check and get back to them.

```json
{json.dumps(inventory_compact, indent=2)}
```

## Your task
Classify each DM and draft a reply (if auto_send=True).

## Hard rules
1. auto_send = True ONLY for: size_question, availability, order_status, general_inquiry
2. auto_send = False ALWAYS for: bulk_inquiry, complaint, influencer, spam
3. Spam gets no reply AND no flag — just skip
4. Flag HIGH priority: bulk_inquiry (revenue opportunity), complaint (churn risk)
5. Flag NORMAL priority: influencer (collab evaluation)
6. reply_text must reference the customer's @username if available
7. NEVER promise a specific delivery date or price discount in auto-replies
8. For out-of-stock availability: offer to notify when restocked (ask them to DM size)
9. Keep all auto-replies under 400 characters (Instagram DM limit is 1000, but shorter = better)
"""

    # Truncate DM text for token efficiency
    dms_compact = [
        {
            "conversation_id": dm["conversation_id"],
            "message_id":      dm["message_id"],
            "user_id":         dm["user_id"],
            "username":        dm.get("username", "customer"),
            "message_text":    (dm.get("message_text") or "")[:200],
            "created_at":      dm.get("created_at", ""),
        }
        for dm in raw_dms
    ]

    user_msg = (
        f"DMs needing reply for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(dms_compact, indent=2)}\n```\n\n"
        "Classify and draft replies for each DM above."
    )

    structured_llm = model.with_structured_output(DmAnalysis)
    analysis: DmAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    auto_count    = sum(1 for d in analysis.decisions if d.auto_send)
    flagged_count = sum(1 for d in analysis.decisions if d.flag_for_human)

    print(
        f"[DM] Analysis complete: {len(analysis.decisions)} DMs — "
        f"{auto_count} auto-send, {flagged_count} flagged. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — send_dm_replies
# ══════════════════════════════════════════════════════════════════════════════

async def send_dm_replies(state: DmAgentState) -> dict:
    """
    Sends auto-replies via social-mcp. Raises alerts for flagged DMs.

    Auto-send: calls send_instagram_dm() for each auto_send=True decision.
    Flagged:   writes AgentAlert so the supervisor surfaces them in the dashboard.

    All processed DMs (sent and flagged) are written to state.dm_replies for
    the run summary and dashboard display.
    """
    analysis = DmAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    dm_replies: list[dict] = []
    alerts:     list[AgentAlert] = []

    # ── Open MCP for sending ───────────────────────────────────────────────────
    client   = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    for d in analysis.decisions:
        if d.category == "spam":
            print(f"[DM] ✗ Skipped spam from @{d.username}")
            continue

        reply_rec = {
            "message_id":       d.message_id,
            "conversation_id":  d.conversation_id,
            "user_id":          d.user_id,
            "username":         d.username,
            "category":         d.category,
            "original_message": d.original_message[:200],
            "reply_text":       d.reply_text,
            "auto_sent":        False,
            "flagged":          d.flag_for_human,
            "sent_at":          None,
        }

        # ── Auto-send path ─────────────────────────────────────────────────────
        if d.auto_send and d.reply_text and "send_instagram_dm" in tool_map:
            try:
                raw = await tool_map["send_instagram_dm"].ainvoke({
                    "user_id": d.user_id,
                    "message": d.reply_text,
                    "brand_id": state["brand_id"]
                })
                result = _parse_mcp_result(raw)

                if isinstance(result, dict) and result.get("success"):
                    reply_rec["auto_sent"] = True
                    reply_rec["sent_at"]   = result.get("sent_at", now_iso)
                    print(
                        f"[DM] ✓ Auto-replied to @{d.username} "
                        f"[{d.category}]: {d.reply_text[:60]}..."
                    )
                else:
                    error = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
                    print(f"[DM] ✗ Send failed for @{d.username}: {error}")
                    alerts.append(AgentAlert(
                        level      = "warning",
                        agent      = "dm_agent",
                        message    = f"DM send FAILED for @{d.username} ({d.category}): {error}",
                        sku        = None,
                        created_at = now_iso,
                    ))

            except Exception as exc:
                print(f"[DM] ✗ Exception sending to @{d.username}: {exc}")

        # ── Flag path ──────────────────────────────────────────────────────────
        if d.flag_for_human:
            alert_level = "warning" if d.flag_priority == "high" else "info"
            alerts.append(AgentAlert(
                level      = alert_level,
                agent      = "dm_agent",
                message    = (
                    f"FLAGGED DM [{d.category.upper()}] from @{d.username}: "
                    f"'{d.original_message[:100]}...' "
                    f"— {d.flag_reason or 'Needs human response.'}"
                ),
                sku        = None,
                created_at = now_iso,
            ))
            print(
                f"[DM] ◔ Flagged @{d.username} [{d.category}] — "
                f"priority: {d.flag_priority}"
            )

        dm_replies.append(reply_rec)

    auto_sent  = sum(1 for r in dm_replies if r["auto_sent"])
    flagged    = sum(1 for r in dm_replies if r["flagged"])

    print(
        f"[DM] Done. {auto_sent} sent, {flagged} flagged, "
        f"{len(alerts)} alerts."
    )

    return {
        "dm_replies": dm_replies,
        "alerts":     alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_dm_graph() -> StateGraph:
    graph = StateGraph(DmAgentState)

    graph.add_node("fetch_dm_data",      fetch_dm_data)
    graph.add_node("load_domain_skill",  load_domain_skill)
    graph.add_node("run_claude_analysis",run_claude_analysis)
    graph.add_node("send_dm_replies",    send_dm_replies)

    graph.add_edge(START,                 "fetch_dm_data")
    graph.add_edge("fetch_dm_data",       "load_domain_skill")
    graph.add_edge("load_domain_skill",   "run_claude_analysis")
    graph.add_edge("run_claude_analysis", "send_dm_replies")
    graph.add_edge("send_dm_replies",     END)

    return graph.compile()


dm_graph = build_dm_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.dm.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — DM Agent Test Run")
        print("═" * 60 + "\n")

        # Simulate Inventory Agent having run (for availability answers)
        mock_inventory = [
            {
                "sku": "FOS-001-S", "product_title": "Olive Cargo Pants",
                "variant_title": "Small", "current_stock": 18,
                "units_per_day": 1.8, "days_of_stock_remaining": 10.0, "urgency": "high",
            },
            {
                "sku": "FOS-001-M", "product_title": "Olive Cargo Pants",
                "variant_title": "Medium", "current_stock": 0,
                "units_per_day": 0.0, "days_of_stock_remaining": 0.0, "urgency": "critical",
            },
        ]

        initial_state: DmAgentState = {
            "brand_id":           os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":         os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot": mock_inventory,
            "raw_dms":            [],
            "skill_content":      "",
            "raw_analysis":       "",
            "dm_replies":         [],
            "alerts":             [],
        }

        result = await dm_graph.ainvoke(initial_state)

        print("\n── DM REPLIES ─────────────────────────────────────────────")
        for reply in result["dm_replies"]:
            status = "✓ SENT" if reply["auto_sent"] else ("◔ FLAGGED" if reply["flagged"] else "○ skipped")
            print(f"\n  {status}  @{reply['username']} [{reply['category']}]")
            if reply.get("reply_text"):
                print(f"  Reply: {reply['reply_text'][:120]}...")

        print("\n── ALERTS ─────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()}: {alert['message'][:120]}")

        print("\n── DONE ───────────────────────────────────────────────────\n")

    asyncio.run(_test_run())