"""
Content Agent — FashionOS Phase 2 Operations
============================================
Reads trend_signals, inventory_snapshot, and pricing_recommendations already
in state (set by Trend, Inventory, and Pricing agents). Selects the best
products to create content for this cycle. Generates Instagram captions and
TikTok scripts using the fashion_content domain skill.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  prepare_content_data    ← Node 1: NO MCP — pure logic on state data.
      │                              Selects up to 5 candidates per run.
      │                              Priority: trending → on-sale → high-velocity.
      │                              Skips: clearance, zero-stock, no-SKU.
      ▼
  load_domain_skill       ← Node 2: load_skill("fashion_content")
      │                              Caption formula, TikTok structure, brand voice.
      ▼
  run_gemini_analysis     ← Node 3: Single structured Gemini call.
      │                              Per candidate: Instagram caption + TikTok script.
      │                              Urdu-English mix, no clichés, PST-aware timing.
      ▼
  write_state_outputs     ← Node 4: Writes content_queue + alerts to state.
      │                              Urgent posts (trending) raise "info" alerts.
      ▼
    END

Selection logic (Node 1):
  Priority 1 — Trending + in stock + not clearance  → urgency="high", post TODAY
    Reads trend_signals where direction in ("rising","peaking") and matched_sku exists.
    Must have current_stock > 5 in inventory_snapshot. Max 3 from this bucket.

  Priority 2 — On markdown + in stock + not trending → urgency="normal", this week
    Reads pricing_recommendations where action="markdown" and auto_executed.
    Not already selected as trending. Max 2 from this bucket.

  Hard skip: clearance_code action, current_stock ≤ 5, no matched SKU in inventory.
  Total cap: 5 candidates per run.

What it generates per candidate:
  Instagram:
    - Full caption (hook → product desc → CTA, 80-150 words)
    - 20-25 hashtags (niche PK + product + occasion + trending)
    - Optimal post time: 20:00 PKT

  TikTok:
    - Hook (0-3s): show outcome first
    - Context (3-8s): occasion or problem setup
    - Reveal (8-20s): details, fabric, price, styling
    - CTA (last 3s): DM / link / stock urgency
    - Optimal post time: 19:00 PKT

  Both: creator notes (what to film/photograph)

No MCP calls — all input data comes from earlier agents in the same run.
Uses Gemini via Vertex AI (Google Cloud requirement) when GOOGLE_CLOUD_PROJECT set.

Standalone test:
  python -m agents.content.graph
"""

import json
import os
from typing import Annotated
import operator
from typing_extensions import TypedDict
from datetime import datetime, timezone

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.skills import load_skill
from agents.state import AgentAlert, InventorySnapshot, PricingRecommendation, TrendSignal

from response_schemas.content_model import ContentPlan, ContentPost

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

if GOOGLE_CLOUD_PROJECT:
    model = init_chat_model("google_vertexai:gemini-2.5-flash")
    print(f"[Content] Using Vertex AI (project={GOOGLE_CLOUD_PROJECT}) ← hackathon mode.")
else:
    model = init_chat_model("google_genai:gemini-2.5-flash-lite")
    print("[Content] GOOGLE_CLOUD_PROJECT not set — using google_genai (local dev mode).")

MAX_CANDIDATES      = int(os.getenv("CONTENT_MAX_CANDIDATES",   "5"))
MAX_TRENDING        = int(os.getenv("CONTENT_MAX_TRENDING",      "3"))
MIN_STOCK_THRESHOLD = int(os.getenv("CONTENT_MIN_STOCK",         "5"))

# ── Subgraph state ─────────────────────────────────────────────────────────────

class ContentAgentState(TypedDict):
    # From parent state (set by prior agents)
    brand_id:   str
    brand_name: str
    products:   list[dict]

    # Read from prior agents — all already in state
    trend_signals:           list[TrendSignal]
    inventory_snapshot:      list[InventorySnapshot]
    pricing_recommendations: list[PricingRecommendation]

    # Node 1 output (internal scratch — LangGraph drops on merge)
    content_candidates: list[dict]

    # Internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → operator.add merges safely with other agents
    content_queue: Annotated[list[dict], operator.add]
    alerts:        Annotated[list[AgentAlert], operator.add]


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — prepare_content_data
# ══════════════════════════════════════════════════════════════════════════════

def prepare_content_data(state: ContentAgentState) -> dict:
    """
    Selects which products deserve content this run. No MCP, no I/O.
    Reads directly from state populated by Inventory, Trend, and Pricing agents.

    Selection algorithm:
      Priority 1: trending + in stock + not clearance  → urgency="high"
      Priority 2: on-sale + in stock + not trending    → urgency="normal"
      Hard skip:  clearance_code, stock ≤ MIN_STOCK_THRESHOLD, no inventory record
      Cap:        MAX_TRENDING from trending, MAX_CANDIDATES total
    """
    # ── Build lookups ─────────────────────────────────────────────────────────
    inv_by_sku: dict[str, InventorySnapshot] = {
        s["sku"]: s
        for s in state.get("inventory_snapshot", [])
        if s.get("sku")
    }

    pricing_by_sku: dict[str, PricingRecommendation] = {
        p["sku"]: p
        for p in state.get("pricing_recommendations", [])
        if p.get("sku")
    }

    # SKUs currently on clearance — skip entirely (don't promote what you're clearing)
    clearance_skus: set[str] = {
        p["sku"]
        for p in state.get("pricing_recommendations", [])
        if p.get("action") == "clearance_code"
    }

    # SKUs with enough stock to promote
    in_stock_skus: set[str] = {
        s["sku"]
        for s in state.get("inventory_snapshot", [])
        if s.get("current_stock", 0) > MIN_STOCK_THRESHOLD
    }

    # Trending → matched_sku, direction in rising/peaking, score ≥ 0.4
    trending_signals: dict[str, TrendSignal] = {}
    for sig in state.get("trend_signals", []):
        sku = sig.get("matched_sku")
        if (
            sku
            and sig.get("direction") in ("rising", "peaking")
            and sig.get("score", 0) >= 0.4
            and sku not in clearance_skus
            and sku in in_stock_skus
        ):
            # Keep highest-scored signal per SKU
            if sku not in trending_signals or sig["score"] > trending_signals[sku]["score"]:
                trending_signals[sku] = sig

    # On-sale SKUs (auto-executed markdowns, discount > 0)
    on_sale_skus: dict[str, PricingRecommendation] = {
        p["sku"]: p
        for p in state.get("pricing_recommendations", [])
        if (
            p.get("action") == "markdown"
            and p.get("discount_pct", 0) > 0
            and p.get("sku") not in clearance_skus
            and p.get("sku") in in_stock_skus
        )
    }

    candidates: list[dict] = []
    seen: set[str] = set()

    # ── Priority 1: Trending ──────────────────────────────────────────────────
    for sku, sig in sorted(trending_signals.items(), key=lambda x: -x[1]["score"]):
        if len(candidates) >= MAX_TRENDING:
            break
        if sku in seen:
            continue

        inv     = inv_by_sku.get(sku, {})
        pricing = pricing_by_sku.get(sku, {})

        candidates.append({
            "sku":                sku,
            "product_title":      inv.get("product_title", ""),
            "variant_title":      inv.get("variant_title", ""),
            "current_stock":      inv.get("current_stock", 0),
            "current_price":      pricing.get("current_price", 0.0),
            "recommended_price":  pricing.get("recommended_price", 0.0),
            "is_trending":        True,
            "trend_keyword":      sig.get("keyword", ""),
            "trend_platform":     sig.get("platform", ""),
            "trend_direction":    sig.get("direction", ""),
            "trend_score":        round(sig.get("score", 0.0), 2),
            "is_on_sale":         sku in on_sale_skus,
            "discount_pct":       on_sale_skus.get(sku, {}).get("discount_pct", 0.0),
            "urgency":            "high",
        })
        seen.add(sku)

    # ── Priority 2: On sale (not already trending) ────────────────────────────
    for sku, pricing in on_sale_skus.items():
        if len(candidates) >= MAX_CANDIDATES:
            break
        if sku in seen:
            continue

        inv = inv_by_sku.get(sku, {})
        candidates.append({
            "sku":               sku,
            "product_title":     inv.get("product_title", ""),
            "variant_title":     inv.get("variant_title", ""),
            "current_stock":     inv.get("current_stock", 0),
            "current_price":     pricing.get("current_price", 0.0),
            "recommended_price": pricing.get("recommended_price", 0.0),
            "is_trending":       False,
            "trend_keyword":     "",
            "trend_platform":    "",
            "trend_direction":   "",
            "trend_score":       0.0,
            "is_on_sale":        True,
            "discount_pct":      pricing.get("discount_pct", 0.0),
            "urgency":           "normal",
        })
        seen.add(sku)

    n_urgent = sum(1 for c in candidates if c["urgency"] == "high")
    n_normal = sum(1 for c in candidates if c["urgency"] == "normal")

    print(
        f"[Content] {len(candidates)} candidates selected: "
        f"{n_urgent} urgent (trending), {n_normal} normal (on-sale)."
    )

    return {"content_candidates": candidates}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: ContentAgentState) -> dict:
    """Loads fashion_content skill: caption formula, TikTok structure, brand voice, posting times."""
    skill = load_skill("fashion_content")
    print("[Content] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_gemini_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_gemini_analysis(state: ContentAgentState) -> dict:
    """
    Single structured Gemini call that generates Instagram captions + TikTok scripts
    for all selected candidates.

    Input: content_candidates (from Node 1) + fashion_content skill + brand context.
    Output: _ContentPlan with one _ContentPost per candidate.

    Not a ReAct loop. All context is in state. One call is enough.
    Uses Vertex AI (google_vertexai:gemini-2.5-flash) when GOOGLE_CLOUD_PROJECT set.
    """
    candidates = state.get("content_candidates", [])

    if not candidates:
        print("[Content] No candidates — skipping content generation.")
        empty = ContentPlan(
            posts=[],
            summary=(
                "No content generated this cycle. "
                "No trending or on-sale products with sufficient stock were found."
            ),
        )
        return {"raw_analysis": empty.model_dump_json()}

    system_prompt = f"""You are the Content Agent for {state['brand_name']}, \
a Pakistani fashion brand operating autonomously via AI.

{state['skill_content']}

## Your task
Generate Instagram captions and TikTok scripts for each product below.
Write content that a real Pakistani fashion brand would actually post — not AI-sounding.

## Brand voice rules (NON-NEGOTIABLE)
- Conversational Urdu-English code-switching is natural and encouraged
  ("yaar", "bilkul", "Eid wali vibes", "must dekho")
- NEVER use: stunning, gorgeous, look no further, must-have, elevate your look
- ALWAYS mention at least one specific: fabric name, cut, or occasion
- "Limited stock" → ONLY write this if current_stock < 20 (check the data)
- CTA must be ONE clear action. Don't list multiple options.

## Trending context
If a product is marked is_trending=True, reference the trend naturally.
Don't say "this is trending" — show it through the hook and context.
Example: 'Cargo pants ka season aa gaya' (not 'Cargo pants are trending right now').

## Sale context
If is_on_sale=True and discount_pct > 0, the sale_mention field must have the
exact price text: 'Now PKR {{recommended_price}} (was PKR {{current_price}})'.'.
Weave this naturally into the TikTok reveal and Instagram caption.

## Scheduling
Instagram: always optimal_post_time_instagram = '20:00 PKT'
TikTok:    always optimal_post_time_tiktok    = '19:00 PKT'
If is_urgent=True, add note that content should go live TODAY at those times.

## Output requirement
Generate one _ContentPost per candidate. Every field is required.
"""

    user_msg = (
        f"Brand: {state['brand_name']}\n\n"
        f"## Content candidates ({len(candidates)} products)\n"
        f"```json\n{json.dumps(candidates, indent=2)}\n```\n\n"
        "Generate full Instagram + TikTok content for each candidate above."
    )

    structured_llm = model.with_structured_output(ContentPlan)
    plan: ContentPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    urgent  = [p for p in plan.posts if p.is_urgent]
    normal  = [p for p in plan.posts if not p.is_urgent]

    print(
        f"[Content] Generated {len(plan.posts)} content pieces: "
        f"{len(urgent)} urgent, {len(normal)} scheduled. "
        f"Summary: {plan.summary}"
    )

    return {"raw_analysis": plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: ContentAgentState) -> dict:
    """
    Converts _ContentPost Pydantic objects → plain dicts for state.content_queue.
    Each product becomes ONE dict containing both Instagram and TikTok content.
    Raises "info" alerts for urgent posts so they surface in the run dashboard.
    """
    plan    = ContentPlan.model_validate_json(state["raw_analysis"])
    now_iso = datetime.now(timezone.utc).isoformat()

    content_queue: list[dict] = []
    alerts:        list[AgentAlert] = []

    for post in plan.posts:
        item = {
            "sku":           post.sku,
            "product_title": post.product_title,
            "variant_title": post.variant_title,
            "is_urgent":     post.is_urgent,
            "status":        "pending",
            "created_at":    now_iso,
            "instagram": {
                "caption":      post.instagram_caption,
                "hashtags":     post.instagram_hashtags,
                "optimal_time": post.optimal_post_time_instagram,
            },
            "tiktok": {
                "script": {
                    "hook":    post.tiktok_hook,
                    "context": post.tiktok_context,
                    "reveal":  post.tiktok_reveal,
                    "cta":     post.tiktok_cta,
                },
                "optimal_time": post.optimal_post_time_tiktok,
            },
            "creator_notes": post.creator_notes,
            "sale_mention":  post.sale_mention,
        }
        content_queue.append(item)

        # Alert for urgent posts so they surface in dashboard + run summary
        if post.is_urgent:
            alerts.append(AgentAlert(
                level      = "info",
                agent      = "content_agent",
                message    = (
                    f"CONTENT READY — POST TODAY: {post.product_title} ({post.sku}). "
                    f"Instagram at {post.optimal_post_time_instagram}, "
                    f"TikTok at {post.optimal_post_time_tiktok}. "
                    f"Creator notes: {post.creator_notes}"
                ),
                sku        = post.sku,
                created_at = now_iso,
            ))

    print(
        f"[Content] Written {len(content_queue)} posts to content_queue, "
        f"{len(alerts)} alerts."
    )

    return {
        "content_queue": content_queue,
        "alerts":        alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_content_graph() -> StateGraph:
    graph = StateGraph(ContentAgentState)

    graph.add_node("prepare_content_data",  prepare_content_data)
    graph.add_node("load_domain_skill",     load_domain_skill)
    graph.add_node("run_gemini_analysis",   run_gemini_analysis)
    graph.add_node("write_state_outputs",   write_state_outputs)

    graph.add_edge(START,                    "prepare_content_data")
    graph.add_edge("prepare_content_data",   "load_domain_skill")
    graph.add_edge("load_domain_skill",      "run_gemini_analysis")
    graph.add_edge("run_gemini_analysis",    "write_state_outputs")
    graph.add_edge("write_state_outputs",    END)

    return graph.compile()


content_graph = build_content_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.content.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Content Agent Test Run")
        print("═" * 60 + "\n")

        # Simulate prior agents having run
        mock_trend_signals: list[TrendSignal] = [
            {
                "keyword":     "cargo pants",
                "platform":    "tiktok",
                "score":       0.82,
                "direction":   "rising",
                "matched_sku": "FOS-001-S",
            },
            {
                "keyword":     "co-ord set",
                "platform":    "instagram",
                "score":       0.65,
                "direction":   "peaking",
                "matched_sku": "FOS-005-M",
            },
        ]

        mock_inventory: list[InventorySnapshot] = [
            {
                "sku":                     "FOS-001-S",
                "product_title":           "Olive Cargo Pants",
                "variant_title":           "Small",
                "current_stock":           18,
                "units_per_day":           1.8,
                "days_of_stock_remaining": 10.0,
                "urgency":                 "high",
            },
            {
                "sku":                     "FOS-005-M",
                "product_title":           "Beige Linen Co-ord Set",
                "variant_title":           "Medium",
                "current_stock":           25,
                "units_per_day":           1.2,
                "days_of_stock_remaining": 20.8,
                "urgency":                 "normal",
            },
            {
                "sku":                     "FOS-002-S",
                "product_title":           "Pink Chiffon Kurta",
                "variant_title":           "Small",
                "current_stock":           40,
                "units_per_day":           0.0,
                "days_of_stock_remaining": 999.0,
                "urgency":                 "normal",
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
                "reason":            "Trending — hold at full price.",
            },
            {
                "sku":               "FOS-002-S",
                "variant_id":        123457,
                "current_price":     1999.0,
                "recommended_price": 1699.0,
                "action":            "markdown",
                "discount_pct":      15.0,
                "reason":            "Dead stock first markdown — 15% off.",
            },
        ]

        initial_state: ContentAgentState = {
            "brand_id":               os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":             os.getenv("BRAND_NAME", "FashionOS Brand"),
            "products":               [],
            "trend_signals":          mock_trend_signals,
            "inventory_snapshot":     mock_inventory,
            "pricing_recommendations":mock_pricing,
            "content_candidates":     [],
            "skill_content":          "",
            "raw_analysis":           "",
            "content_queue":          [],
            "alerts":                 [],
        }

        result = await content_graph.ainvoke(initial_state)

        print("\n── CONTENT QUEUE ──────────────────────────────────────────────")
        for item in result["content_queue"]:
            urgency_tag = "🔴 URGENT" if item["is_urgent"] else "🟡 scheduled"
            print(f"\n  {urgency_tag} — {item['product_title']} ({item['sku']})")
            print(f"  IG time: {item['instagram']['optimal_time']} | TikTok time: {item['tiktok']['optimal_time']}")
            print(f"\n  📸 Instagram caption:")
            print(f"  {item['instagram']['caption'][:200]}...")
            print(f"\n  Hashtags ({len(item['instagram']['hashtags'])}): #{' #'.join(item['instagram']['hashtags'][:6])}...")
            print(f"\n  🎬 TikTok hook: {item['tiktok']['script']['hook']}")
            print(f"  🎬 TikTok CTA:  {item['tiktok']['script']['cta']}")
            print(f"\n  📋 Creator notes: {item['creator_notes']}")
            if item.get("sale_mention"):
                print(f"  💰 Sale: {item['sale_mention']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()} [{alert.get('sku', '—')}]: {alert['message'][:120]}...")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())