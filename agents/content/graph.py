"""
Content Agent — FashionOS Phase 2 Operations (deterministic-split rewrite)
============================================================================
Reads trend_signals, inventory_snapshot, and pricing_recommendations already
in state (set by Trend, Inventory, and Pricing agents). Selects the best
products to create content for this cycle, then computes everything that has
a fixed answer — posting times, sale price text, hashtags — in plain Python.
The LLM only writes the creative parts: caption, TikTok script beats, and
filming notes.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  prepare_content_data  ← Node 1: NO MCP — pure logic on state data.
      │                            Selects up to 5 candidates per run.
      │                            Priority: trending → on-sale → high-velocity.
      │                            Skips: clearance, zero-stock, no-SKU.
      ▼
  compute_content_plan  ← Node 2: PURE PYTHON. Posting times (fixed
      │                            constants), sale_mention (price template),
      │                            hashtags (keyword rules on brand-controlled
      │                            product titles + agents/seasonal.py's
      │                            demand calendar), trigger, is_urgent.
      │                            No LLM — the fashion_content skill already
      │                            gives an exact formula for all of this.
      ▼
  generate_content_copy ← Node 3: THE ONLY LLM CALL. Given the fully
      │                            computed plan, writes per-candidate
      │                            Instagram caption, TikTok hook/context/
      │                            reveal/cta, and creator notes. Loads
      │                            fashion_content skill inline — a sync
      │                            dict lookup doesn't need its own node.
      ▼
  write_state_outputs   ← Node 4: Merges Node 2's plan + Node 3's copy into
      │                            content_queue + alerts. Urgent posts
      │                            (trending) raise an "info" alert.
      ▼
    END

Selection logic (Node 1, unchanged):
  Priority 1 — Trending + in stock + not clearance  → urgency="high", post TODAY
    Reads trend_signals where direction in ("rising","peaking") and matched_sku exists.
    Must have current_stock > 5 in inventory_snapshot. Max 3 from this bucket.

  Priority 2 — On markdown + in stock + not trending → urgency="normal", this week
    Reads pricing_recommendations where action="markdown" and auto_executed.
    Not already selected as trending. Max 2 from this bucket.

  Hard skip: clearance_code action, current_stock ≤ 5, no matched SKU in inventory.
  Total cap: 5 candidates per run.

Deterministic plan (Node 2, new):
  - optimal_post_time_instagram / _tiktok: fixed constants (20:00 / 19:00 PKT),
    configurable via env var — the skill states these exactly, no reason to
    have the LLM re-decide them every run.
  - sale_mention: 'Now PKR {recommended_price} (was PKR {current_price})'
    templated directly from Pricing Agent numbers when is_on_sale.
  - hashtags: broad PK (fixed 5) + product-specific (keyword-matched against
    product_title/variant_title — brand-controlled text, same category of
    parsing as Restock's supplier_type classification) + occasion/style
    (keyword-matched, falling back to the current seasonal context from
    agents/seasonal.py when the title doesn't name an occasion) + niche PK
    (fixed 4) + trend keyword (when trending). Deterministic and consistent
    run to run instead of the LLM re-inventing a hashtag mix each time.
  - trigger: "trending" | "on_sale". is_urgent: mirrors Node 1's urgency bucket.

What Node 3 writes (creative only):
  Instagram caption, TikTok hook/context/reveal/cta, creator notes — the
  same skill-driven brand-voice rules as before, just no longer sharing the
  call with fixed-format fields.

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
from agents.seasonal import current_seasonal_context
from agents.state import AgentAlert, ContentQueueItem, InventorySnapshot, PricingRecommendation, TrendSignal

from response_schemas.content_model import ContentCopyPlan, ContentPlanItem

from dotenv import load_dotenv
load_dotenv()

from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
from langchain_core.messages import HumanMessage, SystemMessage

from langchain_core.utils.function_calling import convert_to_openai_tool



# ── Config ─────────────────────────────────────────────────────────────────────

# GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

# if GOOGLE_CLOUD_PROJECT:
#     model = init_chat_model("google_vertexai:gemini-2.5-flash")
#     print(f"[Content] Using Vertex AI (project={GOOGLE_CLOUD_PROJECT}) ← hackathon mode.")
# else:
#     model = init_chat_model("google_genai:gemini-2.5-flash-lite")
#     print("[Content] GOOGLE_CLOUD_PROJECT not set — using google_genai (local dev mode).")




llm = HuggingFaceEndpoint(
    repo_id="zai-org/GLM-5.2",
    task="text-generation",
    max_new_tokens=2048,
    do_sample=False,
    repetition_penalty=1.03,
    provider="auto"
)

model = ChatHuggingFace(llm=llm)

MAX_CANDIDATES      = int(os.getenv("CONTENT_MAX_CANDIDATES",   "5"))
MAX_TRENDING        = int(os.getenv("CONTENT_MAX_TRENDING",      "3"))
MIN_STOCK_THRESHOLD = int(os.getenv("CONTENT_MIN_STOCK",         "5"))

CONTENT_IG_POST_TIME     = os.getenv("CONTENT_IG_POST_TIME",     "20:00 PKT")
CONTENT_TIKTOK_POST_TIME = os.getenv("CONTENT_TIKTOK_POST_TIME", "19:00 PKT")

# ── Hashtag rules (deterministic — brand-controlled product text + skill's fixed mix) ──

_BROAD_PK_HASHTAGS = ["PakistaniFashion", "PakistaniOutfits", "FashionTikTokPK", "OutfitOfTheDay", "OOTDPakistan"]
_NICHE_PK_HASHTAGS = ["PakistaniFashionBlogger", "DesiStyle", "KarachiStyle", "LahoreStyle"]

_PRODUCT_HASHTAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("cargo",), "CargoPants"),
    (("co-ord", "coord", "co ord"), "CoOrdSet"),
    (("lawn",), "LawnSuit"),
    (("kurta", "kurti"), "KurtiDesign"),
    (("chiffon",), "ChiffonWear"),
    (("dupatta",), "DupattaStyle"),
    (("palazzo",), "PalazzoPants"),
    (("abaya",), "AbayaStyle"),
    (("shalwar", "kameez"), "ShalwarKameez"),
    (("saree", "sari"), "SareeStyle"),
    (("linen",), "LinenWear"),
    (("khaddar",), "KhaddarFabric"),
    (("formal",), "FormalWear"),
]
_PRODUCT_HASHTAG_FALLBACK = "NewDrop"

_OCCASION_HASHTAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("eid",), "EidOutfit"),
    (("wedding", "mehndi", "baraat", "walima"), "WeddingSeason"),
    (("party",), "PartyWear"),
    (("casual",), "CasualWear"),
    (("summer",), "SummerFashion"),
    (("winter",), "WinterFashion"),
]


def _season_hashtag(season_label: str) -> str:
    """Falls back to the active seasonal context when the title names no occasion.
    Substring-matched (not exact) so it stays correct across years without edits."""
    if "eid" in season_label:
        return "EidOutfit"
    if "summer" in season_label:
        return "SummerFashion"
    if "winter" in season_label:
        return "WinterWeddingSeason"
    return "CasualWear"


def _compute_hashtags(
    product_title: str, variant_title: str,
    is_trending: bool, trend_keyword: str,
    season_label: str,
) -> list[str]:
    """
    Deterministic hashtag mix matching the fashion_content skill's exact
    formula: broad PK + product-specific + occasion/style + niche PK +
    trending. Product/occasion matching is keyword-based against
    brand-controlled product text — the same category of parsing Restock's
    supplier_type classifier and Pricing's unit-cost heuristic already do.
    """
    text = f"{product_title} {variant_title}".lower()
    tags: list[str] = list(_BROAD_PK_HASHTAGS)

    product_tags = [tag for keywords, tag in _PRODUCT_HASHTAG_RULES if any(kw in text for kw in keywords)]
    if not product_tags:
        product_tags = [_PRODUCT_HASHTAG_FALLBACK]
    tags.extend(product_tags[:5])

    occasion_tags = [tag for keywords, tag in _OCCASION_HASHTAG_RULES if any(kw in text for kw in keywords)]
    if not occasion_tags:
        occasion_tags = [_season_hashtag(season_label)]
    tags.extend(occasion_tags[:2])

    tags.extend(_NICHE_PK_HASHTAGS)

    if is_trending and trend_keyword:
        trend_tag = "".join(word.capitalize() for word in trend_keyword.split())
        if trend_tag:
            tags.append(trend_tag)

    seen: set[str] = set()
    deduped: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


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

    # Node 2 output (deterministic plan — internal scratch)
    computed_plan: list[dict]

    # Node 3 output (LLM scratch)
    raw_copy: str

    # Final outputs → operator.add merges safely with other agents
    content_queue: Annotated[list[ContentQueueItem], operator.add]
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
# NODE 2 — compute_content_plan (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_content_plan(state: ContentAgentState) -> dict:
    """
    Posting times are fixed constants, sale_mention is a price template, and
    hashtags are generated via keyword rules on brand-controlled product text
    plus the seasonal demand calendar — none of this is free-form customer
    language, so none of it needs an LLM call. Node 3 only ever sees
    numbers/strings that are already final.
    """
    candidates   = state.get("content_candidates", [])
    season_label = current_seasonal_context().get("season_label", "off_season")

    plan: list[dict] = []
    for c in candidates:
        sale_mention = None
        if c.get("is_on_sale") and c.get("discount_pct", 0) > 0:
            sale_mention = f"Now PKR {c['recommended_price']:.0f} (was PKR {c['current_price']:.0f})"

        hashtags = _compute_hashtags(
            product_title=c.get("product_title", ""),
            variant_title=c.get("variant_title", ""),
            is_trending=c.get("is_trending", False),
            trend_keyword=c.get("trend_keyword", ""),
            season_label=season_label,
        )

        item = ContentPlanItem(
            sku=c["sku"], product_title=c.get("product_title", ""), variant_title=c.get("variant_title", ""),
            current_stock=c.get("current_stock", 0),
            current_price=c.get("current_price", 0.0), recommended_price=c.get("recommended_price", 0.0),
            is_trending=c.get("is_trending", False),
            trend_keyword=c.get("trend_keyword", ""), trend_platform=c.get("trend_platform", ""),
            trend_direction=c.get("trend_direction", ""), trend_score=c.get("trend_score", 0.0),
            is_on_sale=c.get("is_on_sale", False), discount_pct=c.get("discount_pct", 0.0),
            sale_mention=sale_mention,
            is_urgent=(c.get("urgency") == "high"),
            trigger=("trending" if c.get("is_trending") else "on_sale"),
            optimal_post_time_instagram=CONTENT_IG_POST_TIME,
            optimal_post_time_tiktok=CONTENT_TIKTOK_POST_TIME,
            hashtags=hashtags,
        )
        plan.append(item.model_dump())

    n_urgent = sum(1 for p in plan if p["is_urgent"])
    print(f"[Content] Plan computed: {len(plan)} candidates ({n_urgent} urgent). Season: {season_label}.")

    return {"computed_plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — generate_content_copy (the ONLY LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_content_copy(state: ContentAgentState) -> dict:
    """
    Every non-creative field — pricing, trend status, sale_mention, is_urgent,
    posting times, hashtags — is already final from Node 2. This call writes
    ONLY: the Instagram caption, the four TikTok script beats, and creator/
    filming notes. Not a ReAct loop — all context is in state, one call is enough.
    """
    plan = state.get("computed_plan", [])

    if not plan:
        print("[Content] No candidates — skipping content generation.")
        empty = ContentCopyPlan(
            items=[],
            summary=(
                "No content generated this cycle. "
                "No trending or on-sale products with sufficient stock were found."
            ),
        )
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_content")   # inlined — a sync dict lookup doesn't need its own node

    compact = [
        {
            "sku": p["sku"], "product_title": p["product_title"], "variant_title": p["variant_title"],
            "current_stock": p["current_stock"],
            "current_price": p["current_price"], "recommended_price": p["recommended_price"],
            "is_trending": p["is_trending"], "trend_keyword": p["trend_keyword"], "trend_direction": p["trend_direction"],
            "is_on_sale": p["is_on_sale"], "discount_pct": p["discount_pct"], "sale_mention": p["sale_mention"],
            "is_urgent": p["is_urgent"],
        }
        for p in plan
    ]

    system_prompt = f"""You are the Content Agent for {state['brand_name']}, \
a Pakistani fashion brand operating autonomously via AI.

{skill_content}

## Your task
Every field below — pricing, trend status, sale_mention, is_urgent — is FINAL, computed by \
deterministic Python logic. Do NOT recompute, second-guess, or contradict any of it. \
Write ONLY, per candidate:
1. instagram_caption
2. tiktok_hook, tiktok_context, tiktok_reveal, tiktok_cta
3. creator_notes

## Brand voice rules (NON-NEGOTIABLE)
- Conversational Urdu-English code-switching is natural and encouraged
  ("yaar", "bilkul", "Eid wali vibes", "must dekho")
- NEVER use: stunning, gorgeous, look no further, must-have, elevate your look
- ALWAYS mention at least one specific: fabric name, cut, or occasion
- If sale_mention is set, use that EXACT price text somewhere in the caption and the TikTok reveal
- If is_trending, reference the trend naturally through the hook/context — don't literally
  say "this is trending". Example: 'Cargo pants ka season aa gaya' not 'Cargo pants are trending'.
- If is_urgent, the creator_notes should note this needs filming today

## Output requirement
Generate one entry per candidate below. Every field is required. Never omit one.
"""

    user_msg = (
        f"Brand: {state['brand_name']}\n\n"
        f"## Content candidates ({len(compact)} products)\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Write the caption, TikTok script, and creator notes for each candidate above."
    )

    dict_schema = convert_to_openai_tool(ContentCopyPlan)

    structured_llm = model.with_structured_output(dict_schema, include_raw=True)
    copy_plan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    parsed = ContentCopyPlan.model_validate(copy_plan["parsed"])

    print(f"[Content] Copy generated for {len(parsed.items)} candidates. Summary: {parsed.summary}")

    return {"raw_copy": parsed.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: ContentAgentState) -> dict:
    """
    Merges Node 2's deterministic plan (posting times, hashtags, sale_mention,
    trigger, is_urgent) with Node 3's creative copy into content_queue.
    Raises "info" alerts for urgent posts so they surface in the run dashboard.
    """
    plan      = state.get("computed_plan", [])
    copy_plan = ContentCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    copy_by_sku = {c.sku: c for c in copy_plan.items}

    content_queue: list[dict] = []
    alerts:        list[AgentAlert] = []

    for p in plan:
        copy = copy_by_sku.get(p["sku"])
        if copy is None:
            print(f"[Content] WARNING: no copy generated for {p['sku']} — skipping.")
            continue

        item = {
            "sku":           p["sku"],
            "product_title": p["product_title"],
            "variant_title": p["variant_title"],
            "is_urgent":     p["is_urgent"],
            "trigger":       p["trigger"],
            "trend_score":   p["trend_score"] if p["is_trending"] else None,
            "discount_pct":  p["discount_pct"],
            "status":        "pending",
            "created_at":    now_iso,
            "instagram": {
                "caption":      copy.instagram_caption,
                "hashtags":     p["hashtags"],
                "optimal_time": p["optimal_post_time_instagram"],
            },
            "tiktok": {
                "script": {
                    "hook":    copy.tiktok_hook,
                    "context": copy.tiktok_context,
                    "reveal":  copy.tiktok_reveal,
                    "cta":     copy.tiktok_cta,
                },
                "optimal_time": p["optimal_post_time_tiktok"],
            },
            "creator_notes": copy.creator_notes,
            "sale_mention":  p["sale_mention"],
        }
        content_queue.append(item)

        # Alert for urgent posts so they surface in dashboard + run summary
        if p["is_urgent"]:
            alerts.append(AgentAlert(
                level      = "info",
                agent      = "content_agent",
                message    = (
                    f"CONTENT READY — POST TODAY: {p['product_title']} ({p['sku']}). "
                    f"Instagram at {p['optimal_post_time_instagram']}, "
                    f"TikTok at {p['optimal_post_time_tiktok']}. "
                    f"Creator notes: {copy.creator_notes}"
                ),
                sku        = p["sku"],
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
    graph.add_node("compute_content_plan",  compute_content_plan)
    graph.add_node("generate_content_copy", generate_content_copy)
    graph.add_node("write_state_outputs",   write_state_outputs)

    graph.add_edge(START,                    "prepare_content_data")
    graph.add_edge("prepare_content_data",   "compute_content_plan")
    graph.add_edge("compute_content_plan",   "generate_content_copy")
    graph.add_edge("generate_content_copy",  "write_state_outputs")
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
            "computed_plan":          [],
            "raw_copy":               "",
            "content_queue":          [],
            "alerts":                 [],
        }

        result = await content_graph.ainvoke(initial_state)

        print("\n── CONTENT QUEUE ──────────────────────────────────────────────")
        for item in result["content_queue"]:
            urgency_tag = "🔴 URGENT" if item["is_urgent"] else "🟡 scheduled"
            print(f"\n  {urgency_tag} [{item['trigger']}] — {item['product_title']} ({item['sku']})")
            print(f"  IG time: {item['instagram']['optimal_time']} | TikTok time: {item['tiktok']['optimal_time']}")
            print(f"\n  📸 Instagram caption:")
            print(f"  {item['instagram']['caption'][:200]}...")
            print(f"\n  Hashtags ({len(item['instagram']['hashtags'])}): #{' #'.join(item['instagram']['hashtags'])}")
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