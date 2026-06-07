"""
Trend Agent — FashionOS Phase 2 Operations
==========================================
Scrapes TikTok + Instagram for Pakistani fashion trend signals, cross-references
with Google Trends, and maps rising trends to SKUs in the product catalog.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  fetch_social_data      ← Node 1: social-mcp + trends-mcp via MultiServerMCPClient.
      │                             Scrapes 2 TikTok + 2 Instagram hashtags.
      │                             Compares top fashion keywords on Google Trends.
      ▼
  load_domain_skill      ← Node 2: load_skill("fashion_trend")
      │                             Platform weighting, trend lifecycle, PK context.
      ▼
  run_gemini_analysis    ← Node 3: Gemini via Vertex AI (Google Cloud requirement).
      │                             Falls back to google_genai for local dev.
      │                             Scores signals 0–1, maps to catalog SKUs,
      │                             flags new product opportunities.
      ▼
  write_state_outputs    ← Node 4: Writes trend_signals + alerts to state.
      │                             operator.add merges safely with other agents.
      ▼
    END

WHY TREND RUNS BEFORE PRICING (not after Restock as handoff doc v4 suggested):
  Pricing Agent reads state.trend_signals to decide hold vs markdown.
  If Trend Agent runs after Pricing, those signals aren't available.
  Supervisor execution order:
    inventory → trend → pricing → restock → summarize

  Trend data doesn't change per Shopify order — running it on every webhook
  would burn Apify quota for zero value. Trend Agent is ONLY activated on:
    - scheduled_run daily  (full sweep)
    - manual               (explicit request)
  Order webhooks stay ["inventory", "pricing", "restock"] — Pricing handles
  empty trend_signals gracefully (already wired from day one).

LLM:
  Uses Gemini via Vertex AI when GOOGLE_CLOUD_PROJECT is set.
  Falls back to google_genai:gemini-2.5-flash-lite for local dev.
  Vertex AI = Google Cloud hackathon requirement satisfied.

Standalone test:
  python -m agents.trend.graph
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
from agents.state import AgentAlert, TrendSignal

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SOCIAL_MCP_URL = os.getenv("SOCIAL_MCP_URL", "http://localhost:8002/mcp")
TRENDS_MCP_URL = os.getenv("TRENDS_MCP_URL", "http://localhost:8003/mcp")

# Vertex AI for Google Cloud hackathon requirement.
# Falls back to google_genai if GOOGLE_CLOUD_PROJECT is not set (local dev).
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

if GOOGLE_CLOUD_PROJECT:
    model = init_chat_model("google_vertexai:gemini-2.5-flash")
    print(f"[Trend] Using Vertex AI (project={GOOGLE_CLOUD_PROJECT}) ← hackathon mode.")
else:
    model = init_chat_model("google_genai:gemini-2.5-flash-lite")
    print("[Trend] GOOGLE_CLOUD_PROJECT not set — using google_genai (local dev mode).")
    print("[Trend] Set GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS for Vertex AI.")

# Pakistani fashion hashtags to monitor — ordered by signal quality.
# We only scrape the top 2 of each platform to control Apify costs.
HASHTAGS_TIKTOK    = ["PakistaniFashion", "FashionTikTokPK", "PakistaniOutfits", "GRWM"]
HASHTAGS_INSTAGRAM = ["PakistaniFashion", "PakistaniOutfits", "OutfitOfTheDay", "modest fashion"]

# Base fashion keywords for Google Trends comparison (Pakistan context)
TREND_KEYWORDS_BASE = ["lawn suit", "co-ord set", "cargo pants", "linen kurta", "modest fashion"]


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _TrendSignalOut(BaseModel):
    keyword:  str
    platform: str = Field(description='"tiktok" | "instagram" | "google_trends"')
    score:    float = Field(ge=0.0, le=1.0, description="Relative trend strength 0–1.")
    direction: str  = Field(description='"rising" | "peaking" | "declining"')
    matched_sku: Optional[str] = Field(
        default=None,
        description=(
            "SKU of the product in the catalog that best matches this trend. "
            "Match on product_title, variant_title, and tags. "
            "None if no catalog product matches."
        ),
    )
    evidence: str = Field(
        description=(
            "1-2 sentence explanation: platform, key engagement numbers, "
            "and the SKU match rationale (or why no match). "
            "Example: 'Cargo pants getting 200k+ views on #PakistaniFashion TikTok — "
            "FOS-001-S (Olive Cargo Pants, Small) matched on title + tag.'"
        )
    )
    is_new_product_opportunity: bool = Field(
        default=False,
        description=(
            "True if this trend has NO matched_sku AND score > 0.5. "
            "Flag to review adding this product to the catalog."
        ),
    )


class _AlertOut(BaseModel):
    level:   str = Field(description='"critical" | "warning" | "info"')
    message: str
    sku:     Optional[str] = Field(default=None)


class _TrendAnalysis(BaseModel):
    trend_signals: list[_TrendSignalOut]
    alerts:        list[_AlertOut]
    summary: str = Field(
        description=(
            "2-3 sentence summary. Lead with the strongest signal. "
            "Example: '3 rising trends detected on TikTok PK. "
            "Cargo pants (FOS-001) and co-ord sets (FOS-005) matched in catalog — "
            "pricing agent should hold these at full price. "
            "1 new opportunity: oversized linen shirt (no catalog match).'"
        )
    )


# ── Subgraph state ─────────────────────────────────────────────────────────────

class TrendAgentState(TypedDict):
    # From parent state (set by Supervisor + Inventory Agent earlier in run)
    brand_id:   str
    brand_name: str
    products:   list[dict]   # Populated by Inventory Agent → propagated by Supervisor

    # Populated by Node 1
    social_signals: list[dict]   # Aggregated TikTok + Instagram posts
    trend_data:     list[dict]   # Google Trends comparison results

    # Agent-internal scratch (LangGraph drops these on merge — not in FashionOSState)
    skill_content: str
    raw_analysis:  str

    # Final outputs → operator.add merges safely with other agents' outputs
    trend_signals: Annotated[list[TrendSignal], operator.add]
    alerts:        Annotated[list[AgentAlert],  operator.add]


# ── Helper: parse MCP result ──────────────────────────────────────────────────

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


# ── Helper: extract catalog keywords for Google Trends scoping ─────────────────

def _extract_product_keywords(products: list[dict]) -> list[str]:
    """
    Pulls searchable keywords from product titles + tags so the Trend Agent
    can search for trends relevant to what's actually in the catalog.
    Skips size, color, and filler words.
    """
    stop_words = {
        "size", "small", "medium", "large", "xlarge", "black", "white", "blue",
        "red", "pink", "green", "with", "and", "the", "for", "new", "pack",
        "free", "set", "suit", "kameez", "shalwar",
    }
    keywords: list[str] = []
    seen: set[str] = set()

    for p in products:
        title = (p.get("title") or "").lower()
        words = [w.strip(",-()") for w in title.split() if len(w) > 4]
        for w in words[:3]:
            if w not in stop_words and w not in seen:
                seen.add(w)
                keywords.append(w)

        tags = (p.get("tags") or "").lower().replace(",", " ").split()
        for t in tags:
            t = t.strip()
            if len(t) > 4 and t not in stop_words and t not in seen:
                seen.add(t)
                keywords.append(t)

    return keywords[:8]   # cap: Google Trends max is 5, but we'll dedupe below


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_social_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_social_data(state: TrendAgentState) -> dict:
    """
    Fetches raw trend signals from social-mcp and trends-mcp.

    Social scraping (via Apify, costs quota):
    - TikTok: top 2 Pakistani fashion hashtags, 15 posts each
    - Instagram: top 2 Pakistani fashion hashtags, 10 posts each

    Google Trends (free, via Pytrends):
    - Compares 5 keywords: 3 base fashion terms + 2 extracted from catalog

    All calls use tool_map.get() — if an MCP tool is missing (stale Docker
    image, server down), that source is skipped and the agent continues with
    whatever data is available.
    """
    all_social_signals: list[dict] = []
    trend_comparison:   list[dict] = []

    # ── Connect to both MCP servers ────────────────────────────────────────────
    social_client = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "streamable_http"}}
    )
    trends_client = MultiServerMCPClient(
        {"trends": {"url": TRENDS_MCP_URL, "transport": "streamable_http"}}
    )

    social_tools = await social_client.get_tools()
    trends_tools = await trends_client.get_tools()

    social_map = {t.name: t for t in social_tools}
    trends_map = {t.name: t for t in trends_tools}

    print(f"[Trend] social-mcp tools: {list(social_map.keys())}")
    print(f"[Trend] trends-mcp tools: {list(trends_map.keys())}")

    # ── TikTok: scrape top 2 hashtags ─────────────────────────────────────────
    if "search_tiktok_hashtag" in social_map:
        for hashtag in HASHTAGS_TIKTOK[:2]:
            try:
                raw = await social_map["search_tiktok_hashtag"].ainvoke(
                    {"hashtag": hashtag, "limit": 15, "geo": "PK"}
                )
                posts = _parse_mcp_result(raw)
                if isinstance(posts, list):
                    all_social_signals.extend(posts)
                    print(f"[Trend] TikTok #{hashtag}: {len(posts)} posts")
            except Exception as e:
                print(f"[Trend] TikTok #{hashtag} failed: {e}")
    else:
        print("[Trend] WARNING: search_tiktok_hashtag not in tool_map — rebuild social-mcp image")

    # ── Instagram: scrape top 2 hashtags ──────────────────────────────────────
    if "search_instagram_hashtag" in social_map:
        for hashtag in HASHTAGS_INSTAGRAM[:2]:
            try:
                raw = await social_map["search_instagram_hashtag"].ainvoke(
                    {"hashtag": hashtag, "limit": 10}
                )
                posts = _parse_mcp_result(raw)
                if isinstance(posts, list):
                    all_social_signals.extend(posts)
                    print(f"[Trend] Instagram #{hashtag}: {len(posts)} posts")
            except Exception as e:
                print(f"[Trend] Instagram #{hashtag} failed: {e}")
    else:
        print("[Trend] WARNING: search_instagram_hashtag not in tool_map — rebuild social-mcp image")

    # ── Google Trends: compare keywords ────────────────────────────────────────
    if "compare_keywords" in trends_map:
        # Blend base keywords with catalog-specific ones, stay within 5-keyword limit
        catalog_kws = _extract_product_keywords(state.get("products", []))
        trend_kws   = list(dict.fromkeys(TREND_KEYWORDS_BASE + catalog_kws))[:5]

        try:
            raw = await trends_map["compare_keywords"].ainvoke(
                {"keywords": trend_kws, "timeframe": "today 1-m", "geo": "PK"}
            )
            comparison = _parse_mcp_result(raw)
            if isinstance(comparison, list):
                trend_comparison = comparison
                print(f"[Trend] Google Trends: {len(comparison)} keywords compared → {[c.get('keyword') for c in comparison]}")
        except Exception as e:
            print(f"[Trend] Google Trends failed: {e}")
    else:
        print("[Trend] WARNING: compare_keywords not in tool_map — rebuild trends-mcp image")

    print(
        f"[Trend] Fetch complete: {len(all_social_signals)} social posts, "
        f"{len(trend_comparison)} trend records."
    )

    return {
        "social_signals": all_social_signals,
        "trend_data":     trend_comparison,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: TrendAgentState) -> dict:
    """Loads fashion_trend skill: platform weighting, lifecycle, PK market context."""
    skill = load_skill("fashion_trend")
    print("[Trend] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_gemini_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_gemini_analysis(state: TrendAgentState) -> dict:
    """
    Single structured Gemini call that:
    1. Aggregates raw social posts into per-hashtag engagement summaries
    2. Cross-references with Google Trends direction data
    3. Scores each trend keyword 0.0–1.0 (relative strength)
    4. Maps each trend to a product SKU in the catalog (matched_sku)
    5. Flags new product opportunities (score > 0.5, no catalog match)

    One structured call — not a ReAct loop. All data is already in state.

    Uses Vertex AI (google_vertexai:gemini-2.5-flash) in production.
    Falls back to google_genai:gemini-2.5-flash-lite for local dev.
    """
    social_signals = state.get("social_signals", [])
    trend_data     = state.get("trend_data", [])
    products       = state.get("products", [])

    if not social_signals and not trend_data:
        print("[Trend] No signal data — skipping analysis.")
        empty = _TrendAnalysis(
            trend_signals=[],
            alerts=[_AlertOut(
                level   = "warning",
                message = (
                    "No trend data fetched this cycle. "
                    "social-mcp or trends-mcp may be unreachable, "
                    "or Apify ran out of quota."
                ),
                sku=None,
            )],
            summary="No trend data available. Check social-mcp and trends-mcp server health.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    # ── Build compact product catalog for SKU matching ─────────────────────────
    catalog: list[dict] = []
    for p in products:
        for v in p.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                catalog.append({
                    "sku":           sku,
                    "product_title": p.get("title", ""),
                    "variant_title": v.get("title", ""),
                    "tags":          p.get("tags", ""),
                })

    # ── Aggregate social posts by hashtag to save tokens ───────────────────────
    # Raw post lists can be 30-50 items; summarise into per-hashtag stats
    agg: dict[str, dict] = {}
    for post in social_signals:
        # Skip error records
        if "error" in post:
            continue

        platform = post.get("platform", "unknown")
        hashtag  = post.get("hashtag", "")
        key      = f"{platform}:{hashtag}"

        if key not in agg:
            agg[key] = {
                "platform":       platform,
                "hashtag":        hashtag,
                "post_count":     0,
                "total_views":    0,
                "total_likes":    0,
                "total_comments": 0,
                "total_shares":   0,
                "sample_captions": [],
            }

        e = agg[key]
        e["post_count"]     += 1
        e["total_views"]    += post.get("views", 0)
        e["total_likes"]    += post.get("likes", 0)
        e["total_comments"] += post.get("comments", 0)
        e["total_shares"]   += post.get("shares", 0)

        text = (post.get("text") or post.get("caption") or "").strip()
        if text and len(e["sample_captions"]) < 3:
            e["sample_captions"].append(text[:150])

    social_summary = list(agg.values())

    # ── System prompt ──────────────────────────────────────────────────────────
    system_prompt = f"""You are the Trend Agent for {state['brand_name']}, \
an autonomous AI fashion brand system.

{state['skill_content']}

## Scoring rubric (0.0 – 1.0)
Score based on engagement volume AND cross-platform confirmation:
  0.8–1.0  Very high engagement, rising fast, 2+ platform signals
  0.5–0.8  Strong — consistent signal on 1 platform OR light on 2
  0.3–0.5  Moderate — single platform, lower engagement
  0.0–0.3  Noise — skip these (don't include in output)

## Direction
  "rising"   → Getting more posts/views over the period
  "peaking"  → At maximum — Google Trends at 80+ and not growing
  "declining" → Engagement falling, past its peak

## SKU matching
Scan the product catalog below. For each trend signal, find the catalog entry
whose product_title + variant_title + tags most closely describes the trend.
  - Match fabric names: lawn, linen, chiffon, cotton, khaddar
  - Match style names: co-ord, kurta, cargo, suit, dupatta, palazzo
  - Match occasion: eid, formal, casual, summer, festive
  - If confidence < 50%, set matched_sku = null

## Alert rules
  "critical": score ≥ 0.8 AND direction = "rising" AND matched_sku is not null
              → A catalog product is going viral RIGHT NOW. Pricing + Inventory must know.
  "info":     score ≥ 0.5 AND matched_sku is null
              → Trend not covered by catalog. Possible new product to add.
  No alerts for score < 0.5.

## Output rules
  - Only include signals with score ≥ 0.3
  - Skip generic hashtags (#OOTD, #fashion) unless engagement is exceptional
  - One signal per meaningful keyword/trend, not one per hashtag
"""

    user_msg = (
        f"Brand: {state['brand_name']}\n\n"
        f"## Social Signals (aggregated)\n"
        f"```json\n{json.dumps(social_summary, indent=2)}\n```\n\n"
        f"## Google Trends (last 30 days, Pakistan)\n"
        f"```json\n{json.dumps(trend_data, indent=2)}\n```\n\n"
        f"## Product Catalog (for SKU matching — {len(catalog)} SKUs)\n"
        f"```json\n{json.dumps(catalog[:50], indent=2)}\n```\n\n"
        "Analyse signals and return structured trend decisions."
    )

    structured_llm = model.with_structured_output(_TrendAnalysis)
    analysis: _TrendAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    rising  = [s for s in analysis.trend_signals if s.direction == "rising"]
    matched = [s for s in analysis.trend_signals if s.matched_sku]
    opps    = [s for s in analysis.trend_signals if s.is_new_product_opportunity]

    print(
        f"[Trend] Analysis complete. "
        f"{len(analysis.trend_signals)} signals: "
        f"{len(rising)} rising, {len(matched)} catalog-matched, "
        f"{len(opps)} new product opportunities. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: TrendAgentState) -> dict:
    """
    Deserialises Pydantic JSON → typed TrendSignal + AgentAlert dicts.
    New product opportunity signals get their own "info" alert.
    operator.add semantics → safe merge with outputs from other agents.
    """
    analysis = _TrendAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    trend_signals: list[TrendSignal] = [
        TrendSignal(
            keyword     = s.keyword,
            platform    = s.platform,
            score       = s.score,
            direction   = s.direction,
            matched_sku = s.matched_sku,
        )
        for s in analysis.trend_signals
    ]

    # Alerts from LLM analysis
    alerts: list[AgentAlert] = [
        AgentAlert(
            level      = a.level,
            agent      = "trend_agent",
            message    = a.message,
            sku        = a.sku,
            created_at = now_iso,
        )
        for a in analysis.alerts
    ]

    # Extra "info" alert per new product opportunity
    for sig in analysis.trend_signals:
        if sig.is_new_product_opportunity:
            alerts.append(AgentAlert(
                level      = "info",
                agent      = "trend_agent",
                message    = (
                    f"NEW PRODUCT OPPORTUNITY: '{sig.keyword}' is trending "
                    f"(score={sig.score:.2f}, {sig.direction}) on {sig.platform} "
                    f"but has NO matching SKU in the current catalog. "
                    f"Evidence: {sig.evidence}"
                ),
                sku        = None,
                created_at = now_iso,
            ))

    print(
        f"[Trend] Written {len(trend_signals)} trend signals, "
        f"{len(alerts)} alerts to state."
    )

    return {
        "trend_signals": trend_signals,
        "alerts":        alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_trend_graph() -> StateGraph:
    graph = StateGraph(TrendAgentState)

    graph.add_node("fetch_social_data",   fetch_social_data)
    graph.add_node("load_domain_skill",   load_domain_skill)
    graph.add_node("run_gemini_analysis", run_gemini_analysis)
    graph.add_node("write_state_outputs", write_state_outputs)

    graph.add_edge(START,                  "fetch_social_data")
    graph.add_edge("fetch_social_data",    "load_domain_skill")
    graph.add_edge("load_domain_skill",    "run_gemini_analysis")
    graph.add_edge("run_gemini_analysis",  "write_state_outputs")
    graph.add_edge("write_state_outputs",  END)

    return graph.compile()


trend_graph = build_trend_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.trend.graph
# (requires social-mcp on :8002 and trends-mcp on :8003)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Trend Agent Test Run")
        print("═" * 60 + "\n")

        initial_state: TrendAgentState = {
            "brand_id":      os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":    os.getenv("BRAND_NAME", "TestBrand"),
            "products":      [],   # Empty in standalone — no SKU matching
            "social_signals":[],
            "trend_data":    [],
            "skill_content": "",
            "raw_analysis":  "",
            "trend_signals": [],
            "alerts":        [],
        }

        result = await trend_graph.ainvoke(initial_state)

        print("\n── TREND SIGNALS ──────────────────────────────────────────────")
        for sig in sorted(result["trend_signals"], key=lambda s: -s["score"]):
            print(
                f"  {sig['platform'].upper():<14} "
                f"score={sig['score']:.2f}  "
                f"{sig['direction']:<10} "
                f"'{sig['keyword']}'  "
                f"→ {sig.get('matched_sku') or '(no catalog match)'}"
            )

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert['sku']}]" if alert.get("sku") else ""
            print(f"  {alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())