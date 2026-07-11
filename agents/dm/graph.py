"""
DM Agent — FashionOS Phase 2 Operations (classification/gating split)
========================================================================
Monitors Instagram DMs. Classification (free-form customer text -> category)
is a real-language-understanding task and stays its own LLM node, same
correction applied to the Returns Agent. Once a category is known, the
auto_send / flag_for_human / flag_priority decision is a FIXED lookup table
straight off the fashion_dm skill's rules — deterministic, computed in
Python. Reply drafting (prose, and only for auto_send=True DMs) is a
separate, later LLM call so no tokens are spent drafting replies for
flagged or spam messages.

Graph topology (5 nodes, sequential):

    START
      │
      ▼
  fetch_dm_data        ← Node 1: social-mcp → get_instagram_dms(limit=30).
      │                           Filters to needs_reply=True only.
      ▼
  classify_dms         ← Node 2: FIRST LLM call, OWN NODE. Real language
      │                           understanding of messy Urdu-English DM
      │                           text -> one of 8 categories. Classification
      │                           only — no gating decision, no reply text.
      ▼
  compute_dm_gating    ← Node 3: PURE PYTHON. category -> auto_send /
      │                           flag_for_human / flag_priority / flag_reason
      │                           via a fixed lookup table (see fashion_dm
      │                           skill's "Category classification" section —
      │                           keep these two in sync). No LLM involved.
      ▼
  generate_dm_replies  ← Node 4: SECOND LLM call. Drafts reply_text ONLY for
      │                           auto_send=True DMs (size_question,
      │                           availability, order_status, general_inquiry).
      │                           Uses inventory_snapshot from state for
      │                           accurate availability answers. No tokens
      │                           spent on flagged/spam DMs.
      ▼
  send_dm_replies      ← Node 5: send_instagram_dm() for each auto_send=True
      │                           item with a reply. Raises AgentAlert for
      │                           flagged DMs. Writes dm_replies + alerts.
      │                           Spam is dropped here — no DB row, no alert.
      ▼
    END

Gating table (Node 3, matches fashion_dm skill exactly):
  size_question / availability / order_status / general_inquiry
      → auto_send=True,  flag_for_human=False
  bulk_inquiry  → auto_send=False, flag_for_human=True,  priority=high
  complaint     → auto_send=False, flag_for_human=True,  priority=high
  influencer    → auto_send=False, flag_for_human=True,  priority=normal
  spam          → auto_send=False, flag_for_human=False (no reply, no flag)

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
from typing import Annotated
import operator

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import AgentAlert, DMReply, InventorySnapshot
from response_schemas.dm_model import DmClassificationBatch, DmReplyCopyPlan

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SOCIAL_MCP_URL = os.getenv("SOCIAL_MCP_URL", "http://localhost:8002/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

DM_FETCH_LIMIT = int(os.getenv("DM_FETCH_LIMIT", "30"))

# Fixed lookup — NOT a judgment call, once category is known. Mirrors the
# fashion_dm skill's "Category classification" rules exactly. If you change
# one, change the other.
_GATING_BY_CATEGORY: dict[str, dict] = {
    "size_question":   {"auto_send": True,  "flag_for_human": False, "flag_priority": None,     "flag_reason": None},
    "availability":    {"auto_send": True,  "flag_for_human": False, "flag_priority": None,     "flag_reason": None},
    "order_status":    {"auto_send": True,  "flag_for_human": False, "flag_priority": None,     "flag_reason": None},
    "general_inquiry": {"auto_send": True,  "flag_for_human": False, "flag_priority": None,     "flag_reason": None},
    "bulk_inquiry":    {"auto_send": False, "flag_for_human": True,  "flag_priority": "high",   "flag_reason": "Bulk/wholesale inquiry — real revenue opportunity, needs human pricing and negotiation."},
    "complaint":       {"auto_send": False, "flag_for_human": True,  "flag_priority": "high",   "flag_reason": "Unhappy customer — auto-reply risks making it worse, needs a human touch."},
    "influencer":      {"auto_send": False, "flag_for_human": True,  "flag_priority": "normal", "flag_reason": "Collab/influencer inquiry — needs human evaluation of fit and terms."},
    "spam":            {"auto_send": False, "flag_for_human": False, "flag_priority": None,     "flag_reason": None},
}
_DEFAULT_GATING = {"auto_send": False, "flag_for_human": True, "flag_priority": "normal", "flag_reason": "Unrecognized category — flagged for manual review."}


# ── Subgraph state ─────────────────────────────────────────────────────────────

class DmAgentState(TypedDict):
    # From parent state
    brand_id:   str
    brand_name: str

    # From Inventory Agent — for availability answers (no extra MCP needed)
    inventory_snapshot: list[InventorySnapshot]

    # Node 1 output (internal scratch)
    raw_dms: list[dict]   # Only needs_reply=True DMs

    # Node 2 output (LLM scratch — flat classification list)
    raw_classifications: str

    # Node 3 output (deterministic gating plan — internal scratch)
    computed_gating: list[dict]

    # Node 4 output (LLM scratch — reply copy)
    raw_copy: str

    # Final outputs → operator.add merges safely with other agents
    dm_replies: Annotated[list[DMReply], operator.add]
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
    """Fetches Instagram DMs from social-mcp. Filters to needs_reply=True only."""
    client   = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

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

    raw_dms = [dm for dm in all_dms if dm.get("needs_reply", False)]

    print(f"[DM] Fetched {len(all_dms)} conversations, {len(raw_dms)} need replies.")

    return {"raw_dms": raw_dms}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — classify_dms (the FIRST LLM call — its own node)
# ══════════════════════════════════════════════════════════════════════════════

async def classify_dms(state: DmAgentState) -> dict:
    """
    Classifies each DM into one of 8 categories via real language understanding
    — not keyword matching. Real customer DMs are messy: Urdu-English code-
    switching, paraphrases, compound questions. This node ONLY classifies —
    no gating decision (Node 3) and no reply text (Node 4).
    """
    raw_dms = state.get("raw_dms", [])

    if not raw_dms:
        print("[DM] No DMs to classify.")
        return {"raw_classifications": DmClassificationBatch(classifications=[]).model_dump_json()}

    skill_content = load_skill("fashion_dm")

    dms_compact = [
        {
            "message_id":      dm["message_id"],
            "conversation_id": dm["conversation_id"],
            "user_id":         dm["user_id"],
            "username":        dm.get("username", "customer"),
            "message_text":    (dm.get("message_text") or "")[:200],
        }
        for dm in raw_dms
    ]

    system_prompt = f"""You are classifying incoming Instagram DMs for {state['brand_name']}, \
a Pakistani fashion brand.

{skill_content}

## Your task
For each DM below, assign exactly ONE category:
size_question | availability | order_status | general_inquiry | bulk_inquiry | complaint | influencer | spam

Use real understanding, not literal keyword matching:
- Handle Urdu-English code-switched text naturally
- Handle paraphrases — a message doesn't need to use the exact trigger words in the skill above
- Handle compound messages — pick the DOMINANT intent if a message touches on two things
- If genuinely ambiguous, prefer general_inquiry over guessing a specific category

## Output requirement
Return exactly one classification per DM below. Never omit one, never invent one that wasn't given.
Echo back message_id, conversation_id, user_id, username, and original_message (the message_text given) unchanged.
"""

    user_msg = (
        f"Classify these {len(dms_compact)} DMs:\n\n"
        f"```json\n{json.dumps(dms_compact, indent=2)}\n```"
    )

    structured_llm = model.with_structured_output(DmClassificationBatch)
    batch: DmClassificationBatch = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[DM] Classified {len(batch.classifications)} / {len(dms_compact)} DMs.")

    return {"raw_classifications": batch.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — compute_dm_gating (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_dm_gating(state: DmAgentState) -> dict:
    """
    category -> auto_send / flag_for_human / flag_priority / flag_reason via
    a fixed lookup table. Once the category is known, this decision has one
    right answer — no judgment left for the LLM to make.
    """
    batch = DmClassificationBatch.model_validate_json(
        state.get("raw_classifications") or '{"classifications": []}'
    )

    gating: list[dict] = []
    for c in batch.classifications:
        rule = _GATING_BY_CATEGORY.get(c.category, _DEFAULT_GATING)
        gating.append({
            "message_id":       c.message_id,
            "conversation_id":  c.conversation_id,
            "user_id":          c.user_id,
            "username":         c.username,
            "original_message": c.original_message,
            "category":         c.category,
            "auto_send":        rule["auto_send"],
            "flag_for_human":   rule["flag_for_human"],
            "flag_priority":    rule["flag_priority"],
            "flag_reason":      rule["flag_reason"],
        })

    n_auto = sum(1 for g in gating if g["auto_send"])
    n_flag = sum(1 for g in gating if g["flag_for_human"])
    n_spam = sum(1 for g in gating if g["category"] == "spam")

    print(f"[DM] Gating computed: {len(gating)} DMs — {n_auto} auto-send, {n_flag} flagged, {n_spam} spam.")

    return {"computed_gating": gating}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — generate_dm_replies (the SECOND LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_dm_replies(state: DmAgentState) -> dict:
    """
    Drafts reply_text ONLY for auto_send=True DMs. Availability questions get
    answered using inventory_snapshot from state — the model gets a compact
    product+stock table so replies are accurate. No tokens spent on flagged
    or spam DMs — their gating is already final from Node 3.
    """
    gating   = state.get("computed_gating", [])
    to_reply = [g for g in gating if g["auto_send"]]

    if not to_reply:
        print("[DM] No auto-send DMs — skipping reply generation.")
        empty = DmReplyCopyPlan(items=[], summary="No DMs required an auto-reply this cycle.")
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_dm")

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
    ][:30]

    system_prompt = f"""You are drafting DM replies for {state['brand_name']}, \
a Pakistani Instagram fashion brand.

{skill_content}

## Current inventory (for availability questions)
Use this ONLY when a customer asks about a specific product's availability.
Cross-reference product names/descriptions from the DM text with this table.
If no match found, reply that you'll check and get back to them.

```json
{json.dumps(inventory_compact, indent=2)}
```

## Your task
Every DM below has already been classified and gated as auto_send=True — that decision \
is final, do not second-guess it. Write ONLY the reply_text for each.

## Hard rules
1. reply_text must reference the customer's @username if available
2. NEVER promise a specific delivery date or price discount
3. For out-of-stock availability: offer to notify when restocked (ask them to DM their size)
4. Keep replies under 400 characters
"""

    user_msg = (
        f"DMs to reply to for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(to_reply, indent=2)}\n```\n\n"
        "Write reply_text for each DM above."
    )

    structured_llm = model.with_structured_output(DmReplyCopyPlan)
    copy_plan: DmReplyCopyPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[DM] Replies drafted for {len(copy_plan.items)} / {len(to_reply)} DMs. Summary: {copy_plan.summary}")

    return {"raw_copy": copy_plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — send_dm_replies
# ══════════════════════════════════════════════════════════════════════════════

async def send_dm_replies(state: DmAgentState) -> dict:
    """
    Sends auto-replies via social-mcp. Raises alerts for flagged DMs. Spam is
    dropped entirely here — no DB row, no alert, matches the "don't persist
    noise" pattern used by the Returns Agent for healthy SKUs.
    """
    gating    = state.get("computed_gating", [])
    copy_plan = DmReplyCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    reply_text_by_id = {i.message_id: i.reply_text for i in copy_plan.items}

    dm_replies: list[DMReply]    = []
    alerts:     list[AgentAlert] = []

    client   = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    for g in gating:
        if g["category"] == "spam":
            print(f"[DM] ✗ Skipped spam from @{g['username']}")
            continue

        reply_text = reply_text_by_id.get(g["message_id"])

        rec: DMReply = {
            "message_id":       g["message_id"],
            "conversation_id":  g["conversation_id"],
            "user_id":          g["user_id"],
            "username":         g["username"],
            "original_message": g["original_message"][:200],
            "category":         g["category"],
            "auto_send":        g["auto_send"],
            "flag_for_human":   g["flag_for_human"],
            "flag_priority":    g["flag_priority"],
            "flag_reason":      g["flag_reason"],
            "reply_text":       reply_text,
            "auto_sent":        False,
            "sent_at":          None,
            "status":           "flagged_open" if g["flag_for_human"] else "send_failed",
        }

        # ── Auto-send path ─────────────────────────────────────────────────────
        if g["auto_send"] and reply_text and "send_instagram_dm" in tool_map:
            try:
                raw = await tool_map["send_instagram_dm"].ainvoke({
                    "user_id":  g["user_id"],
                    "message":  reply_text,
                    "brand_id": state["brand_id"],
                })
                result = _parse_mcp_result(raw)

                if isinstance(result, dict) and result.get("success"):
                    rec["auto_sent"] = True
                    rec["sent_at"]   = result.get("sent_at", now_iso)
                    rec["status"]    = "auto_sent"
                    print(f"[DM] ✓ Auto-replied to @{g['username']} [{g['category']}]: {reply_text[:60]}...")
                else:
                    error = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
                    print(f"[DM] ✗ Send failed for @{g['username']}: {error}")
                    rec["status"] = "send_failed"
                    alerts.append(AgentAlert(
                        level="warning", agent="dm_agent",
                        message=f"DM send FAILED for @{g['username']} ({g['category']}): {error}",
                        sku=None, created_at=now_iso,
                    ))
            except Exception as exc:
                print(f"[DM] ✗ Exception sending to @{g['username']}: {exc}")
                rec["status"] = "send_failed"

        # ── Flag path ──────────────────────────────────────────────────────────
        if g["flag_for_human"]:
            alerts.append(AgentAlert(
                level="warning" if g["flag_priority"] == "high" else "info",
                agent="dm_agent",
                message=(
                    f"FLAGGED DM [{g['category'].upper()}] from @{g['username']}: "
                    f"'{g['original_message'][:100]}...' — {g['flag_reason']}"
                ),
                sku=None, created_at=now_iso,
            ))
            print(f"[DM] ◔ Flagged @{g['username']} [{g['category']}] — priority: {g['flag_priority']}")

        dm_replies.append(rec)

    auto_sent = sum(1 for r in dm_replies if r["auto_sent"])
    flagged   = sum(1 for r in dm_replies if r["flag_for_human"])

    print(f"[DM] Done. {auto_sent} sent, {flagged} flagged, {len(alerts)} alerts.")

    return {"dm_replies": dm_replies, "alerts": alerts}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_dm_graph() -> StateGraph:
    graph = StateGraph(DmAgentState)

    graph.add_node("fetch_dm_data",       fetch_dm_data)
    graph.add_node("classify_dms",        classify_dms)
    graph.add_node("compute_dm_gating",   compute_dm_gating)
    graph.add_node("generate_dm_replies", generate_dm_replies)
    graph.add_node("send_dm_replies",     send_dm_replies)

    graph.add_edge(START,                 "fetch_dm_data")
    graph.add_edge("fetch_dm_data",       "classify_dms")
    graph.add_edge("classify_dms",        "compute_dm_gating")
    graph.add_edge("compute_dm_gating",   "generate_dm_replies")
    graph.add_edge("generate_dm_replies", "send_dm_replies")
    graph.add_edge("send_dm_replies",     END)

    return graph.compile()


dm_graph = build_dm_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.dm.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — DM Agent Test Run")
        print("═" * 60 + "\n")

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
            "brand_id":            os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":          os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":  mock_inventory,
            "raw_dms":             [],
            "raw_classifications": "",
            "computed_gating":     [],
            "raw_copy":            "",
            "dm_replies":          [],
            "alerts":              [],
        }

        result = await dm_graph.ainvoke(initial_state)

        print("\n── DM REPLIES ─────────────────────────────────────────────")
        for reply in result["dm_replies"]:
            print(f"\n  [{reply['status'].upper()}]  @{reply['username']} [{reply['category']}]")
            if reply.get("reply_text"):
                print(f"  Reply: {reply['reply_text'][:120]}...")

        print("\n── ALERTS ─────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()}: {alert['message'][:120]}")

        print("\n── DONE ───────────────────────────────────────────────────\n")

    asyncio.run(_test_run())