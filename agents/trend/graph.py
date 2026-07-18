"""
Trend Agent — Autonomous ReAct Version (memory + alert-intelligence + catalog-search pass)
==============================================================================================
Replaces the fixed 4-node sequential graph with a LangGraph ReAct agent.

Session update — ACTIVE CATALOG SEARCH (this pass):
  SKU matching used to work by dumping the ENTIRE catalog as a JSON blob
  straight into the system prompt, truncated at catalog[:50] — any brand
  with a bigger catalog silently lost SKUs past the first 50 with no
  visibility that it happened, and the agent had to eyeball-match keywords
  against a wall of text instead of actively looking anything up.

  SKU matching itself stays an LLM job — matching organic, evolving slang
  ("cargo pants", "co-ord set") to product attributes is genuine language
  judgment, not a fixed lookup table the way Restock's supplier
  classification is. What changed is HOW the agent accesses the catalog:
  a local search_catalog(query) tool, bound via closure to the FULL
  (untruncated) catalog for this run, doing fuzzy matching (token
  containment + character similarity, stdlib difflib — no new deps) and
  returning scored candidates. The agent calls it per keyword instead of
  holding the whole catalog in context — same "give it a tool, don't just
  prompt-dump it" principle every MCP-backed node already follows, and it
  removes the truncation bug for free.

Session update — ALERT INTELLIGENCE (prior pass):
  Alert eligibility (critical/info thresholds) and history-aware duplicate-
  alert suppression are computed deterministically in Python
  (compute_trend_alerts) instead of self-applied by the LLM. is_new_product_
  opportunity and score_delta are both derived, not self-reported.

Session update — MEMORY (prior pass):
  trend_signals are persisted to Postgres. Node 1 (fetch_trend_history)
  loads the last few days of signals for this brand before the ReAct loop
  runs, so the agent has real history instead of starting cold.

Old approach (hardcoded):
  fetch_social_data (fixed 2 TikTok hashtags + 2 IG hashtags, fixed Google Trends keywords)
  → load_domain_skill
  → run_gemini_analysis (single structured call, one shot)
  → write_state_outputs

Current approach:
  fetch_trend_history    (Node 1 — pure DB read, no MCP, no LLM)
  → run_react_agent      (Node 2 — ReAct agent, full tool control, incl. a
      local search_catalog tool bound to the full catalog. Produces ONLY
      trend_signals: score, direction, matched_sku, evidence.)
  → compute_trend_alerts (Node 3 — pure Python. Critical/info thresholds,
      history-aware duplicate suppression, is_new_product_opportunity,
      score_delta. Every threshold is a fixed number stated once.)
  → write_state_outputs  (Node 4 — type-safe passthrough.)

Agent tools:
  social-mcp: search_tiktok_hashtag, search_instagram_hashtag, get_trending_tiktok_sounds
  trends-mcp: get_trend_data, get_related_queries, compare_keywords
  local:      search_catalog (fuzzy match against the brand's full catalog —
              not an MCP call, pure in-memory computation, bound per run)
  (DM tools excluded — they need brand_id and are irrelevant to trend research)

No hardcoded hashtag lists. No fixed iteration count. Scoring, direction,
SKU matching, and search strategy remain LLM/ReAct judgment.

Graph topology (4 nodes):
  START → fetch_trend_history → run_react_agent → compute_trend_alerts
        → write_state_outputs → END

Requires LangGraph >= 0.2.7 for response_format on create_react_agent.
"""

import json
import operator
import os
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Annotated, Optional

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langchain.agents import create_agent
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import AgentAlert, TrendSignal
from response_schemas.trend_model import TrendFindings, TrendSignalOut

load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SOCIAL_MCP_URL = os.getenv("SOCIAL_MCP_URL", "http://localhost:8002/mcp")
TRENDS_MCP_URL = os.getenv("TRENDS_MCP_URL", "http://localhost:8003/mcp")

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

# if GOOGLE_CLOUD_PROJECT:
#     model = init_chat_model("google_vertexai:gemini-2.5-flash")
#     print(f"[Trend] Using Vertex AI (project={GOOGLE_CLOUD_PROJECT}) ← hackathon mode.")
# else:
#     model = init_chat_model("google_genai:gemini-2.5-flash-lite")
#     print("[Trend] GOOGLE_CLOUD_PROJECT not set — using google_genai (local dev).")

from langchain_sambanova import ChatSambaNova

model = ChatSambaNova(
    model="Meta-Llama-3.3-70B-Instruct",
    max_tokens=1024,
    temperature=0.7,
    top_p=0.01,
    # other params...
)

TREND_HISTORY_LOOKBACK_DAYS = int(os.getenv("TREND_HISTORY_LOOKBACK_DAYS", "3"))
TREND_REALERT_SCORE_DELTA   = float(os.getenv("TREND_REALERT_SCORE_DELTA", "0.15"))

# Fixed thresholds — stated once, applied deterministically in compute_trend_alerts.
CRITICAL_SCORE_FLOOR = float(os.getenv("TREND_CRITICAL_SCORE_FLOOR", "0.8"))
INFO_SCORE_FLOOR      = float(os.getenv("TREND_INFO_SCORE_FLOOR",      "0.5"))

CATALOG_MATCH_FLOOR = float(os.getenv("TREND_CATALOG_MATCH_FLOOR", "0.15"))  # below this, don't even return it
CATALOG_MATCH_CONFIDENCE_HINT = 0.5  # documented match confidence the agent should treat as "good enough"


# ── Subgraph state ─────────────────────────────────────────────────────────────
# Supervisor still passes social_signals/trend_data/skill_content in initial state
# (see supervisor.py run_trend_agent). Those keys are ignored — TypedDict doesn't
# enforce at runtime, LangGraph just drops unknown keys on ainvoke.

class TrendAgentState(TypedDict):
    brand_id:   str
    brand_name: str
    products:   list[dict]   # From Inventory Agent via Supervisor

    trend_history: list[dict]   # Node 1 output — recent signals for this brand

    raw_findings: str           # Node 2 output — serialized TrendFindings JSON (signals only)
    agent_error:  Optional[str] # Node 2 output — set if the ReAct loop itself failed

    trends_context: dict        # Node 3 output — {keyword.lower(): google_trends_data}

    computed_signals: list[dict]   # Node 4 output — signals + is_new_product_opportunity + score_delta
    computed_alerts:  list[dict]   # Node 4 output — {level, message, sku} dicts

    # operator.add → safe merge when Supervisor combines agent outputs
    trend_signals: Annotated[list[TrendSignal], operator.add]
    alerts:        Annotated[list[AgentAlert],  operator.add]


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _build_catalog(products: list[dict]) -> list[dict]:
    """
    Compact catalog — backing data for the search_catalog tool, NOT embedded
    into the prompt as text anymore. No truncation: the tool operates over
    the full list, so brands with more than 50 SKUs no longer silently lose
    matching candidates.
    """
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
    return catalog


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", text.lower()).strip()


def _catalog_match_score(query: str, candidate_text: str) -> float:
    """
    Fuzzy match score 0.0-1.0. Token containment (how many query words
    appear in the candidate) dominates, with a character-level similarity
    ratio (difflib.SequenceMatcher) as a secondary signal — fashion keyword
    matching cares more about term presence ("cargo", "co-ord") than raw
    edit distance, since candidate text ("Olive Cargo Pants - Small") is
    usually longer and phrased differently than the search query
    ("cargo pants").
    """
    q_norm = _normalize(query)
    c_norm = _normalize(candidate_text)
    if not q_norm or not c_norm:
        return 0.0

    q_tokens = [t for t in q_norm.split() if len(t) > 2]  # skip tiny/noise words
    if not q_tokens:
        return 0.0

    contained   = sum(1 for t in q_tokens if t in c_norm)
    token_score = contained / len(q_tokens)
    char_score  = SequenceMatcher(None, q_norm, c_norm).ratio()

    return round(0.75 * token_score + 0.25 * char_score, 3)


def _make_search_catalog_tool(catalog: list[dict]):
    """
    Builds a search_catalog tool bound (via closure) to this run's full
    product catalog. Local computation, not an MCP call — no I/O needed, so
    it stays synchronous.
    """
    @tool
    def search_catalog(query: str, limit: int = 5) -> list[dict]:
        """
        Search the brand's product catalog for items matching a trending
        keyword or phrase (e.g. "cargo pants", "co-ord set", "lawn suit",
        "eid outfit"). Searches the FULL catalog, not a sample. Returns up
        to `limit` best matches sorted by confidence, each with sku,
        product_title, variant_title, tags, and match_confidence (0.0-1.0).
        An empty list means no reasonable match exists in the catalog —
        treat that as matched_sku=null (a new product opportunity), don't
        guess a SKU that isn't actually a good fit.
        """
        capped = max(1, min(limit, 10))
        scored: list[dict] = []
        for item in catalog:
            text  = f"{item['product_title']} {item['variant_title']} {item.get('tags', '')}"
            score = _catalog_match_score(query, text)
            if score > CATALOG_MATCH_FLOOR:
                scored.append({**item, "match_confidence": score})
        scored.sort(key=lambda x: -x["match_confidence"])
        return scored[:capped]

    return search_catalog


async def _fetch_recent_trend_signals(brand_id: str, lookback_days: int) -> list[dict]:
    """
    Reads trend signals raised in the last N days so the ReAct agent can
    compare against its own recent findings instead of starting cold every
    run, and so Node 3 can enforce duplicate-alert suppression.

    IMPORTANT: uses a fresh NullPool engine created inside THIS event loop.
    Reusing db.session.AsyncSessionLocal's module-level pooled engine here
    reproduces the Celery/Windows ProactorEventLoop bug (pooled connections
    bound to the wrong event loop). Reference: agents/inventory/graph.py
    ::_fetch_pending_restocks.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from db import crud as db_crud

    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
    )
    engine  = create_async_engine(database_url, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    history: list[dict] = []
    try:
        async with Session() as session:
            records = await db_crud.get_recent_trend_signals(session, brand_id=brand_id, days=lookback_days)
            for rec in records:
                history.append({
                    "keyword":     rec.keyword,
                    "platform":    rec.platform,
                    "score":       rec.score,
                    "direction":   rec.direction,
                    "matched_sku": rec.matched_sku,
                    "seen_at":     rec.created_at.isoformat(),
                })
    except Exception as exc:
        print(f"[Trend] History lookup failed (non-fatal): {exc}")
    finally:
        await engine.dispose()

    return history


def _latest_per_keyword(history: list[dict]) -> list[dict]:
    """
    Collapses raw history to the single most recent reading per
    (keyword, platform) — pure grouping/aggregation, same category of
    Python job as Returns' Counter aggregation.
    """
    latest: dict[tuple[str, str], dict] = {}
    for h in history:
        key = (h["keyword"].lower(), h["platform"])
        if key not in latest or h["seen_at"] > latest[key]["seen_at"]:
            latest[key] = h
    return sorted(latest.values(), key=lambda h: -h["score"])


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_trend_history
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_trend_history(state: TrendAgentState) -> dict:
    """
    Loads the last TREND_HISTORY_LOOKBACK_DAYS of trend signals for this
    brand so the ReAct agent has real memory instead of starting cold.
    Runs before the tool loop — no MCP calls here, just Postgres.
    """
    raw_history   = await _fetch_recent_trend_signals(state["brand_id"], TREND_HISTORY_LOOKBACK_DAYS)
    trend_history = _latest_per_keyword(raw_history)

    print(
        f"[Trend] Loaded {len(trend_history)} recent signals "
        f"(last {TREND_HISTORY_LOOKBACK_DAYS} days) as history context."
    )

    return {"trend_history": trend_history}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — run_react_agent
# ══════════════════════════════════════════════════════════════════════════════

async def run_react_agent(state: TrendAgentState) -> dict:
    """
    The ReAct agent has full tool autonomy. It:
      1. Picks its own starting hashtags based on brand context
      2. Searches TikTok and Instagram
      3. Evaluates engagement quality
      4. Retries with different hashtags if results are thin
      5. Cross-references with Google Trends when social signals are strong
      6. Calls search_catalog(keyword) to actively look up SKU matches
         against the FULL catalog, instead of eyeballing a static dump
      7. Compares fresh findings against its own recent history to score/
         describe movement accurately
      8. Decides when it has enough data to stop
      9. Produces a TrendFindings via response_format — signals ONLY.
         Alert eligibility is computed downstream (Node 3), not here.
    """
    # ── Get tools from social-mcp only ────────────────────────────────────────
    # trends-mcp is intentionally excluded from the ReAct loop. Google Trends
    # cross-referencing now runs in a fixed Node 3 (cross_reference_trends)
    # AFTER the agent loop, freeing ~2-3 tool-call budget slots for more
    # social scraping — which is where the real signal quality comes from.
    social_client = MultiServerMCPClient(
        {"social": {"url": SOCIAL_MCP_URL, "transport": "http"}}
    )

    social_tools = await social_client.get_tools()

    # Exclude DM tools — brand_id required, irrelevant to trend research.
    # Include batch tools so the agent can sweep multiple hashtags in one call.
    scraping_tools = [
        t for t in social_tools
        if t.name in (
            "search_tiktok_hashtag", "search_tiktok_hashtags_batch",
            "search_instagram_hashtag", "get_trending_tiktok_sounds",
        )
    ]

    skill_content = load_skill("fashion_trend")
    catalog       = _build_catalog(state.get("products", []))
    trend_history = state.get("trend_history", [])

    # search_catalog is bound per-run to the FULL catalog via closure — not
    # an MCP tool, pure local computation, no truncation.
    search_catalog_tool = _make_search_catalog_tool(catalog)
    all_tools = scraping_tools + [search_catalog_tool]

    print(f"[Trend] ReAct tools: {[t.name for t in all_tools]}")

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

### Step 3 — Stop when satisfied (read carefully — these are hard rules)

Hard retry cap: You may retry a **bad hashtag at most 3 times total** across both
platforms combined. After 3 failed/thin attempts, move on — do NOT keep retrying.

Stop iterating when ANY of these is true:
  - You have 2+ good signals with engagement above thresholds AND you have called
    search_catalog for each of them (preferred exit — quality run)
  - You have made 10+ tool calls total, regardless of signal quality (budget exit —
    write whatever you have and stop, even if signals are thin)

Do NOT keep searching indefinitely. If you have SOME good signals, stop and write them.
A partial result is always better than hitting the recursion ceiling with nothing.
IMPORTANT: You do NOT have access to compare_keywords, get_trend_data, or any Google
Trends tool. Those run automatically in a fixed Python step AFTER your loop completes.
Focus only on: TikTok/Instagram scraping + search_catalog. That's your entire toolkit.

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
Your catalog has {len(catalog)} SKUs, searchable via the search_catalog(query, limit) tool
— you do NOT have the full list in this prompt, call the tool instead:
- For every trend signal you're keeping, call search_catalog with the keyword itself
  (try the plain keyword first — e.g. "cargo pants", "co-ord set"). If that returns
  nothing useful, try a related fabric/style/occasion term from the skill above.
- search_catalog runs over the FULL catalog and returns each candidate's
  match_confidence (0.0-1.0) — trust these results rather than guessing from memory.
- If the top result's match_confidence is >= {CATALOG_MATCH_CONFIDENCE_HINT}, use its
  sku as matched_sku.
- If search_catalog returns nothing, or the best match_confidence is below
  {CATALOG_MATCH_CONFIDENCE_HINT}, set matched_sku to null — this is a genuine new
  product opportunity, not something to force-match.
- You can call search_catalog more than once per keyword with different phrasing if
  the first attempt doesn't turn up a confident match.

## RECENT SIGNAL HISTORY (last {TREND_HISTORY_LOOKBACK_DAYS} days, most recent reading per keyword)
{json.dumps(trend_history, indent=2) if trend_history else "No history yet — this is either the first run or nothing was flagged recently."}

Use this to score and describe movement accurately — you do NOT need to decide
whether to raise an alert, that's handled automatically downstream from your
score/direction/matched_sku:
- If a keyword below was already seen recently, compare your fresh research
  against that reading. Is engagement actually higher, lower, or about the
  same right now? Set direction and score based on what you find NOW, and
  mention the comparison explicitly in evidence if it moved meaningfully.
  Example: "Score up from 0.58 two days ago to 0.81 now — accelerating fast."
- If a keyword isn't in this history at all, it's newly discovered this run —
  research and score it normally, no special caveat needed.
- Treat history as a reference point, not ground truth to copy blindly —
  always verify with a fresh search this run rather than assuming yesterday's
  numbers still hold.
"""

    user_message = (
        f"Research trending Pakistani fashion signals for {state['brand_name']}. "
        f"Choose your own hashtags. Iterate until you have strong, well-supported data. "
        f"Use search_catalog to actively look up SKU matches for each keyword you're "
        f"keeping — don't guess. Discard thin or irrelevant hashtag results. Compare "
        f"findings against the recent signal history to describe movement accurately."
    )

    # ── Build and run ReAct agent ──────────────────────────────────────────────
    # response_format: after the agent finishes its tool loop, LangGraph makes
    # one final structured LLM call using the full conversation history.
    # Result lands in result["structured_response"] as a TrendFindings instance.
    agent = create_agent(
        model           = model,
        tools           = all_tools,
        system_prompt   = system_prompt,
        response_format = TrendFindings,
    )

    agent_error: Optional[str] = None

    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config={"recursion_limit": 50},   # ~25 tool-call iterations max; was 20 (too tight)
        )
        findings: TrendFindings = result["structured_response"]

    except Exception as exc:
        print(f"[Trend] ReAct agent error: {exc}")
        agent_error = str(exc)
        findings = TrendFindings(
            trend_signals=[],
            summary=(
                f"Trend agent encountered an error: {exc}. "
                "Check social-mcp (:8002) and trends-mcp (:8003) health."
            ),
        )

    rising  = [s for s in findings.trend_signals if s.direction == "rising"]
    matched = [s for s in findings.trend_signals if s.matched_sku]

    print(
        f"[Trend] ReAct complete: {len(findings.trend_signals)} signals | "
        f"{len(rising)} rising | {len(matched)} catalog-matched. "
        f"Summary: {findings.summary}"
    )

    return {"raw_findings": findings.model_dump_json(), "agent_error": agent_error}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — cross_reference_trends (fixed Python, no LLM — calls trends-mcp)
# ══════════════════════════════════════════════════════════════════════════════

async def cross_reference_trends(state: TrendAgentState) -> dict:
    """
    Calls trends-mcp's compare_keywords on the agent's top-scored keywords.
    Runs OUTSIDE the ReAct loop, so zero node budget is consumed by this step.

    Frees up ~2-3 tool-call slots that the agent previously burned calling
    compare_keywords / get_trend_data mid-loop, while still enriching every
    signal with Google Trends data before compute_trend_alerts runs.
    Non-fatal: rate-limit or network error → empty dict, pipeline continues.
    """
    raw = state.get("raw_findings") or '{"trend_signals": [], "summary": ""}'
    findings = TrendFindings.model_validate_json(raw)

    if not findings.trend_signals:
        return {"trends_context": {}}

    # Deduplicate and take the top-5 keywords by score (Google Trends API limit)
    seen: set[str] = set()
    keywords: list[str] = []
    for s in sorted(findings.trend_signals, key=lambda x: -x.score):
        kw = s.keyword.lower()
        if kw not in seen:
            seen.add(kw)
            keywords.append(s.keyword)
        if len(keywords) == 5:
            break

    print(f"[Trend] Google Trends cross-reference: {keywords}")

    try:
        trends_client = MultiServerMCPClient(
            {"trends": {"url": TRENDS_MCP_URL, "transport": "http"}}
        )
        trends_tools = await trends_client.get_tools()
        compare_tool  = next((t for t in trends_tools if t.name == "compare_keywords"), None)

        if not compare_tool:
            print("[Trend] compare_keywords not found in trends-mcp — skipping.")
            return {"trends_context": {}}

        raw_result = await compare_tool.ainvoke({"keywords": keywords})

        # langchain_mcp_adapters returns the tool output as a JSON string or a list
        if isinstance(raw_result, str):
            result_list = json.loads(raw_result)
        elif isinstance(raw_result, list):
            result_list = raw_result
        else:
            result_list = []

        context = {
            r["keyword"].lower(): r
            for r in result_list
            if isinstance(r, dict) and "keyword" in r
        }
        print(f"[Trend] Google Trends enriched {len(context)}/{len(keywords)} keywords.")

    except Exception as exc:
        print(f"[Trend] Google Trends cross-reference failed (non-fatal): {exc}")
        context = {}

    return {"trends_context": context}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — compute_trend_alerts (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_trend_alerts(state: TrendAgentState) -> dict:
    """
    Pure Python. Every threshold here (critical score floor, info score
    floor, re-alert delta) is a fixed number stated once, not re-decided by
    an LLM every run. is_new_product_opportunity and score_delta are both
    fully derivable from fields the ReAct agent already produced — computed
    here instead of self-reported, so they can't drift from the rule that
    defines them.

    Dedup uses the persisted history from Node 1: a keyword that was
    ALREADY critical-eligible last time and hasn't moved by at least
    TREND_REALERT_SCORE_DELTA gets downgraded to an "info" alert instead of
    a repeat "critical" — this is now an enforced rule, not a prompt
    suggestion the agent could ignore.
    """
    findings = TrendFindings.model_validate_json(
        state.get("raw_findings") or '{"trend_signals": [], "summary": ""}'
    )
    history_by_key = {
        (h["keyword"].lower(), h["platform"]): h
        for h in state.get("trend_history", [])
    }
    # Google Trends enrichment from Node 3 (cross_reference_trends) — keyed by keyword.lower()
    trends_context = state.get("trends_context", {})

    computed_signals: list[dict] = []
    alerts: list[dict] = []
    n_suppressed = 0

    for s in findings.trend_signals:
        key   = (s.keyword.lower(), s.platform)
        prior = history_by_key.get(key)

        is_new_opportunity = s.matched_sku is None and s.score >= INFO_SCORE_FLOOR
        is_critical_eligible = (
            s.score >= CRITICAL_SCORE_FLOOR
            and s.direction == "rising"
            and s.matched_sku is not None
        )

        score_delta: Optional[float] = None
        prior_was_critical = False
        if prior:
            score_delta = round(s.score - prior["score"], 2)
            prior_was_critical = (
                prior["score"] >= CRITICAL_SCORE_FLOOR
                and prior["direction"] == "rising"
                and prior["matched_sku"] is not None
            )

        # Enrich evidence with Google Trends data from Node 3 (if available)
        gt = trends_context.get(s.keyword.lower())
        gt_note = (
            f" | Google Trends (PK): avg={gt.get('avg_interest', '?')}, "
            f"latest={gt.get('latest_interest', '?')}, direction={gt.get('direction', '?')}"
        ) if gt else ""

        computed_signals.append({
            "keyword": s.keyword, "platform": s.platform, "score": s.score,
            "direction": s.direction, "matched_sku": s.matched_sku,
            "evidence": s.evidence + gt_note,
            "is_new_product_opportunity": is_new_opportunity, "score_delta": score_delta,
        })

        # ── Critical alert (rising + matched + high score) ──────────────────
        if is_critical_eligible:
            suppress = (
                prior_was_critical
                and score_delta is not None
                and score_delta < TREND_REALERT_SCORE_DELTA
            )
            if suppress:
                n_suppressed += 1
                alerts.append({
                    "level": "info",
                    "message": (
                        f"'{s.keyword}' still trending on {s.platform} "
                        f"(score {s.score:.2f}, matched to {s.matched_sku}) — no significant "
                        f"change since last check (Δ{score_delta:+.2f}). {s.evidence}"
                    ),
                    "sku": s.matched_sku,
                })
            else:
                if score_delta is not None and score_delta > 0:
                    accel_note = f" Score up {score_delta:+.2f} since last seen."
                elif score_delta is None:
                    accel_note = " First time seeing this signal."
                else:
                    accel_note = ""
                alerts.append({
                    "level": "critical",
                    "message": (
                        f"TRENDING NOW: '{s.keyword}' (score {s.score:.2f}, rising) on "
                        f"{s.platform} — matched to {s.matched_sku}.{accel_note} {s.evidence}"
                    ),
                    "sku": s.matched_sku,
                })

        # ── New product opportunity (info) ───────────────────────────────────
        if is_new_opportunity:
            alerts.append({
                "level": "info",
                "message": (
                    f"NEW PRODUCT OPPORTUNITY: '{s.keyword}' trending "
                    f"(score={s.score:.2f}, {s.direction}) on {s.platform} "
                    f"— no catalog match. Evidence: {s.evidence}"
                ),
                "sku": None,
            })

    # ── Node 2's own failure becomes an operational warning alert here ────────
    agent_error = state.get("agent_error")
    if agent_error:
        alerts.append({
            "level": "warning",
            "message": (
                f"Trend agent failed this run: {agent_error}. "
                "Check social-mcp (:8002) and trends-mcp (:8003) health."
            ),
            "sku": None,
        })

    n_critical = sum(1 for a in alerts if a["level"] == "critical")
    print(
        f"[Trend] Alerts computed: {len(computed_signals)} signals → "
        f"{len(alerts)} alerts ({n_critical} critical, {n_suppressed} duplicate critical suppressed)."
    )

    return {"computed_signals": computed_signals, "computed_alerts": alerts}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: TrendAgentState) -> dict:
    """
    Converts Node 3's plain dicts into typed TrendSignal + AgentAlert state
    objects. All judgment happened in Node 2, all rule application happened
    in Node 3 — this node is now a type-safe passthrough, the same shape as
    every other agent's final write node. operator.add semantics → safe
    merge with other agents' outputs.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    trend_signals: list[TrendSignal] = [
        TrendSignal(
            keyword=s["keyword"], platform=s["platform"], score=s["score"],
            direction=s["direction"], matched_sku=s["matched_sku"], evidence=s["evidence"],
            is_new_product_opportunity=s["is_new_product_opportunity"],
            score_delta=s["score_delta"],
        )
        for s in state.get("computed_signals", [])
    ]

    alerts: list[AgentAlert] = [
        AgentAlert(
            level=a["level"], agent="trend_agent", message=a["message"],
            sku=a["sku"], created_at=now_iso,
        )
        for a in state.get("computed_alerts", [])
    ]

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

    graph.add_node("fetch_trend_history",    fetch_trend_history)
    graph.add_node("run_react_agent",        run_react_agent)
    graph.add_node("cross_reference_trends", cross_reference_trends)
    graph.add_node("compute_trend_alerts",   compute_trend_alerts)
    graph.add_node("write_state_outputs",    write_state_outputs)

    graph.add_edge(START,                     "fetch_trend_history")
    graph.add_edge("fetch_trend_history",     "run_react_agent")
    graph.add_edge("run_react_agent",         "cross_reference_trends")
    graph.add_edge("cross_reference_trends",  "compute_trend_alerts")
    graph.add_edge("compute_trend_alerts",    "write_state_outputs")
    graph.add_edge("write_state_outputs",     END)

    return graph.compile()


trend_graph = build_trend_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test
# python -m agents.trend.graph
# (requires social-mcp on :8002 and trends-mcp on :8003; DB history lookup is
#  non-fatal if Postgres isn't reachable locally — it just logs and continues
#  with an empty history)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Trend Agent (ReAct + memory + alerts + catalog search) Test Run")
        print("═" * 60 + "\n")

        # Small mock catalog so search_catalog has something real to match
        # against when running this file standalone.
        mock_products = [
            {
                "title": "Olive Cargo Pants", "tags": "bottoms, casual, unisex",
                "variants": [{"sku": "FOS-001-S", "title": "Small"}],
            },
            {
                "title": "Beige Linen Co-ord Set", "tags": "co-ord, linen, summer",
                "variants": [{"sku": "FOS-005-M", "title": "Medium"}],
            },
            {
                "title": "Pink Chiffon Kurta", "tags": "kurta, formal, chiffon",
                "variants": [{"sku": "FOS-002-S", "title": "Small"}],
            },
        ]

        initial_state: TrendAgentState = {
            "brand_id":         os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":       os.getenv("BRAND_NAME", "TestBrand"),
            "products":         mock_products,
            "trend_history":    [],
            "raw_findings":     "",
            "agent_error":      None,
            "trends_context":   {},
            "computed_signals": [],
            "computed_alerts":  [],
            "trend_signals":    [],
            "alerts":           [],
        }

        result = await trend_graph.ainvoke(initial_state)

        print("\n── TREND SIGNALS ──────────────────────────────────────────────")
        for sig in sorted(result["trend_signals"], key=lambda s: -s["score"]):
            delta_tag = f"  Δ{sig['score_delta']:+.2f}" if sig.get("score_delta") is not None else "  (new)"
            print(
                f"  {sig['platform'].upper():<14} "
                f"score={sig['score']:.2f}{delta_tag}  "
                f"{sig['direction']:<10} "
                f"'{sig['keyword']}'  "
                f"→ {sig.get('matched_sku') or '(no catalog match)'}"
            )
            print(f"    evidence: {sig.get('evidence', '')[:120]}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert['sku']}]" if alert.get("sku") else ""
            print(f"  {alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())