"""
Trend Agent — Autonomous ReAct Version
========================================
Replaces the fixed 4-node sequential graph with a LangGraph ReAct agent.

Old approach (hardcoded):
  fetch_social_data (fixed 2 TikTok hashtags + 2 IG hashtags, fixed Google Trends keywords)
  → load_domain_skill
  → run_gemini_analysis (single structured call, one shot)
  → write_state_outputs

New approach (autonomous):
  ReAct agent with full tool control:
  - Agent decides which Pakistani fashion hashtags to try based on brand + catalog context
  - Evaluates engagement quality per hashtag (views, likes, post count)
  - Retries with different hashtags if data is thin or irrelevant
  - Cross-references with Google Trends on its own initiative
  - Iterates until it has strong, well-supported signals or has exhausted search space
  → write_state_outputs (unchanged)

Agent tools (from MCP servers):
  social-mcp: search_tiktok_hashtag, search_instagram_hashtag, get_trending_tiktok_sounds
  trends-mcp: get_trend_data, get_related_queries, compare_keywords
  (DM tools excluded — they need brand_id and are irrelevant to trend research)

No hardcoded hashtag lists. No fixed iteration count.
The LLM controls everything.

Graph topology (2 nodes):
  START → run_react_agent → write_state_outputs → END

Requires LangGraph >= 0.2.7 for response_format on create_react_agent.
"""

import json
import operator
import os
from datetime import datetime, timezone
from typing import Annotated, Optional

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langchain.agents import create_agent
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import AgentAlert, TrendSignal
from response_schemas.trend_model import TrendAnalysis, TrendAlertOut, TrendSignalOut

load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SOCIAL_MCP_URL = os.getenv("SOCIAL_MCP_URL", "http://localhost:8002/mcp")
TRENDS_MCP_URL = os.getenv("TRENDS_MCP_URL", "http://localhost:8003/mcp")

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

if GOOGLE_CLOUD_PROJECT:
    model = init_chat_model("google_vertexai:gemini-2.5-flash")
    print(f"[Trend] Using Vertex AI (project={GOOGLE_CLOUD_PROJECT}) ← hackathon mode.")
else:
    model = init_chat_model("google_genai:gemini-2.5-flash-lite")
    print("[Trend] GOOGLE_CLOUD_PROJECT not set — using google_genai (local dev).")




# ── Subgraph state ─────────────────────────────────────────────────────────────
# Supervisor still passes social_signals/trend_data/skill_content in initial state
# (see supervisor.py run_trend_agent). Those keys are ignored — TypedDict doesn't
# enforce at runtime, LangGraph just drops unknown keys on ainvoke.

class TrendAgentState(TypedDict):
    brand_id:   str
    brand_name: str
    products:   list[dict]   # From Inventory Agent via Supervisor

    raw_analysis: str        # Internal: serialized _TrendAnalysis JSON

    # operator.add → safe merge when Supervisor combines agent outputs
    trend_signals: Annotated[list[TrendSignal], operator.add]
    alerts:        Annotated[list[AgentAlert],  operator.add]


# ── Helper ─────────────────────────────────────────────────────────────────────

def _build_catalog(products: list[dict]) -> list[dict]:
    """Compact catalog for SKU matching in the agent prompt."""
    catalog = []
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
    return catalog[:50]


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — run_react_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_react_agent(state: TrendAgentState) -> dict:
    """
    The ReAct agent has full tool autonomy. It:
      1. Picks its own starting hashtags based on brand context
      2. Searches TikTok and Instagram
      3. Evaluates engagement quality
      4. Retries with different hashtags if results are thin
      5. Cross-references with Google Trends when social signals are strong
      6. Decides when it has enough data to stop
      7. Produces a _TrendAnalysis via response_format (structured output call
         happens automatically after the agent finishes its tool loop)
    """
    # ── Get tools from both MCP servers ───────────────────────────────────────
    social_client = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "http"}}
    )
    trends_client = MultiServerMCPClient(
        {"trends": {"url": TRENDS_MCP_URL, "transport": "http"}}
    )

    social_tools = await social_client.get_tools()
    trends_tools = await trends_client.get_tools()

    # Exclude DM tools — they require brand_id and are not relevant here.
    # Trend Agent only needs the scraping + trends tools.
    scraping_tools = [
        t for t in social_tools
        if t.name in ("search_tiktok_hashtag", "search_instagram_hashtag", "get_trending_tiktok_sounds")
    ]
    all_tools = scraping_tools + list(trends_tools)

    print(f"[Trend] ReAct tools: {[t.name for t in all_tools]}")

    skill_content = load_skill("fashion_trend")
    catalog       = _build_catalog(state.get("products", []))

    # ── System prompt — gives the agent its mission and decision framework ─────
    # This prompt replaces hardcoded hashtag lists and fixed iteration counts.
    # The agent reads this and decides what to do autonomously.
    system_prompt = f"""You are the autonomous Trend Agent for {state['brand_name']}, \
a Pakistani fashion brand running on FashionOS.

Your mission: find the strongest real-time trending fashion signals for the Pakistani market.
You control all research decisions. No hashtags are given to you — you choose.

{skill_content}

## TOOL LOOP — how to operate

### Step 1 — Choose starting hashtags
Think about what Pakistani fashion audiences search on TikTok and Instagram.
Consider starting points (not exhaustive — you decide which to actually try):
  PakistaniFashion, PakistaniOutfits, FashionTikTokPK, GRWM, LawnSuit,
  CoordSet, PakistaniWear, EidOutfit, KurtiDesign, AbaayaStyle,
  ModestFashionPK, DesiFashion, SummerOutfitPK, ShalwarKameez, CargoPantsPK,
  WomenFashionPakistan, OOTDPakistan, PakistaniWomenFashion
Pick the 2-3 that best match {state['brand_name']}'s catalog and try those first.

### Step 2 — Evaluate each result immediately
After every search_tiktok_hashtag or search_instagram_hashtag call, check:

GOOD SIGNAL (keep and build on it):
  TikTok:    5+ posts returned AND total views across posts > 10,000
  Instagram: 5+ posts returned AND total likes across posts > 500

THIN/BAD SIGNAL (discard, try a different hashtag):
  < 5 posts returned
  Total engagement near zero
  Posts look like spam or unrelated content (check captions)

If bad: choose a MORE SPECIFIC or DIFFERENT hashtag and retry immediately.
Do NOT include thin data in your final analysis.

### Step 3 — Cross-reference with Google Trends
Once you have 2+ good social signals, use compare_keywords() or get_related_queries()
to verify in Pakistan search volume. This confirms the trend is real, not just viral noise.
direction="rising" + avg_interest > 30 = strong confirmation.

### Step 4 — Stop when satisfied
Stop iterating when ANY of these is true:
  - You have 3-5 strong signals with engagement above thresholds
  - You have searched 6-8 total hashtags (enough breadth)
  - You have confirmed 2+ rising trends via Google Trends cross-reference

Do NOT keep searching indefinitely. Quality over quantity.

## SCORING
0.8-1.0  Very high engagement, rising, confirmed on 2+ platforms
0.5-0.8  Strong on 1 platform, or moderate on 2
0.3-0.5  Moderate, single platform, lower engagement
< 0.3    Noise — exclude entirely

## DIRECTION
"rising"   → engagement growing over the period, or latest > 4-period lookback
"peaking"  → at maximum, not growing
"declining" → falling from peak

## SKU MATCHING
For every trend signal, scan the catalog below.
Match on: fabric names (lawn, linen, chiffon, khaddar, cotton),
          style (co-ord, kurta, cargo, palazzo, dupatta, suit),
          occasion (eid, formal, casual, summer, festive, mehndi).
Set matched_sku to the best matching SKU string, or null if confidence < 50%.

## ALERT RULES
critical: score ≥ 0.8 AND direction="rising" AND matched_sku is not null
info:     score ≥ 0.5 AND matched_sku is null (new product opportunity)
No alerts for score < 0.5.

## PRODUCT CATALOG ({len(catalog)} SKUs)
{json.dumps(catalog, indent=2)}
"""

    user_message = (
        f"Research trending Pakistani fashion signals for {state['brand_name']}. "
        f"Choose your own hashtags. Iterate until you have strong, well-supported data. "
        f"Match every signal to the product catalog. Discard thin or irrelevant hashtag results."
    )

    # ── Build and run ReAct agent ──────────────────────────────────────────────
    # response_format: after the agent finishes its tool loop, LangGraph makes
    # one final structured LLM call using the full conversation history.
    # Result lands in result["structured_response"] as a _TrendAnalysis instance.
    agent = create_agent(
        model           = model,
        tools           = all_tools,
        prompt          = system_prompt,
        response_format = TrendAnalysis,
    )

    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config={"recursion_limit": 50},   # ~25 tool-call iterations max
        )
        analysis: TrendAnalysis = result["structured_response"]

    except Exception as exc:
        print(f"[Trend] ReAct agent error: {exc}")
        analysis = TrendAnalysis(
            trend_signals=[],
            alerts=[TrendAlertOut(
                level   = "warning",
                message = (
                    f"Trend agent failed: {exc}. "
                    "Check social-mcp (:8002) and trends-mcp (:8003) health."
                ),
                sku=None,
            )],
            summary="Trend agent encountered an error. Check MCP server health.",
        )

    rising  = [s for s in analysis.trend_signals if s.direction == "rising"]
    matched = [s for s in analysis.trend_signals if s.matched_sku]
    opps    = [s for s in analysis.trend_signals if s.is_new_product_opportunity]

    print(
        f"[Trend] ReAct complete: {len(analysis.trend_signals)} signals | "
        f"{len(rising)} rising | {len(matched)} catalog-matched | "
        f"{len(opps)} new product opportunities. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — write_state_outputs (identical to v1 — supervisor contract unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: TrendAgentState) -> dict:
    """
    Deserializes _TrendAnalysis JSON → typed TrendSignal + AgentAlert dicts.
    New product opportunity signals get their own "info" alert.
    operator.add semantics → safe merge with other agents' outputs.
    """
    analysis = TrendAnalysis.model_validate_json(state["raw_analysis"])
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
                    f"NEW PRODUCT OPPORTUNITY: '{sig.keyword}' trending "
                    f"(score={sig.score:.2f}, {sig.direction}) on {sig.platform} "
                    f"— no catalog match. Evidence: {sig.evidence}"
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

    graph.add_node("run_react_agent",     run_react_agent)
    graph.add_node("write_state_outputs", write_state_outputs)

    graph.add_edge(START,                 "run_react_agent")
    graph.add_edge("run_react_agent",     "write_state_outputs")
    graph.add_edge("write_state_outputs", END)

    return graph.compile()


trend_graph = build_trend_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test
# python -m agents.trend.graph
# (requires social-mcp on :8002 and trends-mcp on :8003)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Trend Agent (ReAct) Test Run")
        print("═" * 60 + "\n")

        initial_state: TrendAgentState = {
            "brand_id":      os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":    os.getenv("BRAND_NAME", "TestBrand"),
            "products":      [],   # Empty = no SKU matching, agent still finds trends
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