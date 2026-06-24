"""
FashionOS Deep Agent Supervisor — Multi-Tenant
================================================
SHORT-TERM:  Automatic via Deep Agents + thread_id. No MemorySaver needed.
             thread_id = f"{brand_id}:{session_id}" — cross-tenant isolation.

LONG-TERM:   StoreBackend keyed by brand_id namespace.
             /memories/AGENTS.md → persists across ALL conversations per brand.
             Agent edits this file as it learns. Seeded on first brand creation.

EPHEMERAL:   StateBackend() for /workspace/ scratch. Gone after conversation.
"""

import os
import asyncio
from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend, FilesystemBackend
from deepagents.backends.utils import create_file_data
from dotenv import load_dotenv
from langgraph.store.redis.aio import AsyncRedisStore
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from deep_agents.subagents.inventory_agent import build_inventory_subagent
from deep_agents.subagents.trend_agent import build_trend_subagent
from deep_agents.subagents.pricing_agent import build_pricing_subagent
from deep_agents.subagents.marketing_agent import build_marketing_subagent   
from deep_agents.tools.db_tools import get_db_tools
from deep_agents.subagents.content_agent import build_content_subagent

from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

llm = HuggingFaceEndpoint(
    repo_id="deepseek-ai/DeepSeek-R1-0528",
    task="text-generation",
    max_new_tokens=512,
    do_sample=False,
    repetition_penalty=1.03,
    provider="auto",  # let Hugging Face choose the best provider for you
)

chat_model = ChatHuggingFace(llm=llm)

load_dotenv()

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
SOCIAL_MCP_URL  = os.getenv("SOCIAL_MCP_URL",  "http://localhost:8002/mcp")
TRENDS_MCP_URL  = os.getenv("TRENDS_MCP_URL",  "http://localhost:8003/mcp")
ADS_MCP_URL     = os.getenv("ADS_MCP_URL",     "http://localhost:8004/mcp")   
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
BASE_DIR        = Path(__file__).parent.resolve()
SKILLS_DIR      = BASE_DIR / "skills"

# ── Redis store — one instance for whole process ───────────────────────────────
_store: AsyncRedisStore | None = None

async def _get_store() -> AsyncRedisStore:
    global _store
    if _store is None:
        _store = AsyncRedisStore(redis_url=REDIS_URL)
        await _store.setup()
    return _store


_checkpointer: AsyncRedisSaver | None = None

async def _get_checkpointer() -> AsyncRedisSaver:
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = AsyncRedisSaver(redis_url=REDIS_URL)
        await _checkpointer.asetup()
        print("[Checkpointer] ✓ AsyncRedisSaver ready")
    return _checkpointer

# ── Per-brand agent cache ──────────────────────────────────────────────────────
_agent_cache: dict[str, object] = {}


# ── Brand memory seeding ───────────────────────────────────────────────────────

def _seed_agents_md(brand_id: str, brand_name: str) -> str:
    return f"""# FashionOS Brand Memory — {brand_name}

## Brand Identity
- brand_id: {brand_id}
- brand_name: {brand_name}
- platform: Shopify + Meta Ads + Instagram
- currency: PKR (Pakistani Rupee)
- market: Pakistani fashion e-commerce

## Owner Preferences
<!-- Update when you learn something new about the brand owner.
Examples:
- prefers: bullet-point summaries only
- alert_channel: WhatsApp for critical alerts
- name: Tayyab -->

## Brand Rules
<!-- Overrides of global FashionOS defaults.
Examples:
- min_margin_floor: 38%
- price_endings: always PKR X99 or X499
- no_ad_budget_increase_on: Fridays -->

## Supplier Notes
<!-- Example: primary_supplier: Ahmed at Shadman Market, 5-day lead time -->

## Seasonal Patterns
<!-- Example: eid_velocity_multiplier: 3x normal (2 weeks before Eid) -->

## Past Decisions Log
<!-- Agent logs major decisions to avoid repeating bad ones -->
"""


async def _ensure_brand_seeded(brand_id: str, brand_name: str, store: AsyncRedisStore) -> None:
    namespace = (brand_id,)
    key       = "/AGENTS.md"

    existing   = await store.aget(namespace, key)
    needs_seed = existing is None

    if not needs_seed and existing:
        file_data = existing.value or {}
        content   = file_data.get("content", "")
        if isinstance(content, list):
            content = "\n".join(content)
        if "brand_id  :" in content or "brand_name:" in content:
            needs_seed = True
            print(f"[Memory] ↺ Re-seeding AGENTS.md for brand={brand_id} (stale format)")

    if needs_seed:
        await store.aput(namespace, key, create_file_data(_seed_agents_md(brand_id, brand_name)))
        print(f"[Memory] ✓ Seeded AGENTS.md for brand={brand_id}")


# ── System prompt ──────────────────────────────────────────────────────────────

_PROMPT_BASE = """\
You are FashionOS Supervisor — the autonomous AI brain of a Pakistani Shopify fashion brand.

## Memory

### Long-term (persists across ALL conversations)
/memories/AGENTS.md is injected at startup. Contains brand identity, owner preferences,
rules, suppliers, seasonal patterns, and past decisions.

You MUST update it when you learn ANYTHING new — including:
- Owner's name, nickname, or personal preferences
- Brand rule changes or new decisions
- Supplier or pricing updates

How to update (ALWAYS read the file first to get exact text):
  read_file("/memories/AGENTS.md")          ← get exact current content
  edit_file("/memories/AGENTS.md", exact_old_text, new_text)

IMPORTANT: The old_text you pass to edit_file MUST be character-for-character identical
to what you just read. Copy-paste the line, do not retype it.

### Short-term (this conversation only)
Conversation history is automatic — no action needed.

### Operational data
DB tools give you latest pipeline results (inventory, alerts, pricing, content).

## Tool strategy
- Existing data (fast)  → DB tools
- Fresh live analysis   → delegate to subagents
- Learn something new   → read_file then edit_file on /memories/AGENTS.md

### Subagent call guide

inventory-agent:
  task("Run full inventory analysis for brand_id={brand_id}")
  → returns stock levels, velocity, urgency per SKU, dead stock, size anomalies

trend-agent:
  task(
      name="trend-agent",
      task=(
          "Research trending Pakistani fashion signals for {brand_name}. "
          "Catalog: {compact_catalog_json}"
      )
  )
  → returns scored trend signals, catalog matches, new product opportunities
  → build compact_catalog_json from get_inventory_status() results:
    [{{"sku": s["sku"], "product_title": s["product"], "variant_title": s["variant"]}}
     for s in inventory["skus"]]
  → always call get_inventory_status() first to get the catalog before calling trend-agent


pricing-agent:
  # Always call after inventory-agent and trend-agent so it has full context.
  task(
      name="pricing-agent",
      task=(
          "Run full pricing analysis for {brand_name} (brand_id={brand_id}). "
          "inventory_snapshot: {inventory_json} "
          "trend_signals: {trend_signals_json} "
          "Fetch prices, decide, execute approved actions, return analysis."
      )
  )
  → auto-executes markdowns, increases, and clearance codes within safe thresholds
  → returns what ran (executed=True) vs what needs approval (auto_execute=False)
  → check failed_count in result — any > 0 means Shopify write errors occurred


marketing-agent:
  # Always call AFTER inventory-agent, trend-agent, AND pricing-agent.
  # Marketing needs clearance flags from pricing to avoid running ads on cleared stock.
  task(
      name="marketing-agent",
      task=(
          "Run Meta ad campaign analysis for {brand_name} (brand_id={brand_id}). "
          "inventory_snapshot: {inventory_json} "
          "trend_signals: {trend_signals_json} "
          "pricing_recommendations: {pricing_json} "
          "Fetch campaign data, make decisions, execute approved actions, return analysis."
      )
  )
  → auto-executes: pause (OOS/clearance/very_low_roas), decrease_budget (organic_viral/low_roas ≤30%)
  → pending approval: increase_budget (any %), activate (any condition)
  → budget changes rounded to PKR 50, min PKR 200, max ±30% per cycle
  → check failed_count in result — any > 0 means Meta API errors occurred
  → non-compliant campaign names (no SKU match) are held — flag to founder for renaming

  Build {pricing_json} from pricing-agent result decisions array.
  Build {inventory_json} and {trend_signals_json} from prior agents in this run.
  If any upstream result is missing, call get_inventory_status() as fallback for inventory.


content-agent:
  # Call AFTER inventory, trend, pricing, AND marketing agents.
  # Needs clearance flags (pricing) to skip contradictions,
  # and campaign status (marketing) for content-ad sync.
  task(
      name="content-agent",
      task=(
          "Generate content plan for {brand_name} (brand_id={brand_id}). "
          "current_date: {YYYY-MM-DD} "
          "inventory_snapshot: {inventory_json} "
          "trend_signals: {trend_signals_json} "
          "pricing_recommendations: {pricing_json} "
          "marketing_actions: {marketing_json} "
          "return_insights: {return_insights_json} "
          "Select candidates, generate Instagram + TikTok content, return ContentPlan."
      )
  )
  → priority_today_skus = what to film and post TODAY
  → posts = full captions + TikTok scripts + shot lists per SKU
  → fatigue_skips = why any eligible SKU was excluded (check here before re-running)
  → pass return_insights=[] if returns-agent hasn't run yet (gracefully handled)

  Build {marketing_json} from marketing-agent result decisions array (or [] if unavailable).
  Build {return_insights_json} from returns-agent result return_insights (or [] if unavailable).
  Always inject current_date as YYYY-MM-DD string.


## Full daily pipeline order
  1. inventory-agent   → inventory_snapshot
  2. trend-agent       → trend_signals (pass catalog from inventory-agent)
  3. pricing-agent     → pricing_recommendations (pass both above)
  4. marketing-agent   → marketing_analysis (pass all three above)
  5. content-agent     → content_plan (pass all four above + return_insights if available)

Run 1→5 in sequence. Each agent's output feeds the next.
Content is last because it benefits from all upstream context — especially clearance flags
(pricing) and campaign status (marketing) to avoid contradictory signals.



## Output format
✘ CRITICAL  (action needed today)
⚠ WARNING   (action needed this week)
✔ HEALTHY    (no action needed)

Always include real numbers (stock, velocity, PKR, days, ROAS).

## Hard rules
1. Never call Shopify or Meta APIs directly — delegate to subagents.
2. Never guess at numbers — always call a tool first.
3. /memories/AGENTS.md overrides all global defaults for this brand.
4. Never write to /skills/ — read-only.
5. Always pass brand_id=BRAND_ID to every DB tool call.
6. When updating /memories/AGENTS.md, ALWAYS read it first to get exact line content.
7. marketing-agent must ALWAYS run after pricing-agent — it needs clearance flags.
"""

def _build_prompt(brand_id: str, brand_name: str) -> str:
    header = (
        f"## Active Brand\n"
        f"- brand_id   : {brand_id}\n"
        f"- brand_name : {brand_name}\n"
        f"- Rule       : Always pass brand_id=\"{brand_id}\" to every DB tool call.\n\n"
    )
    return header + _PROMPT_BASE.replace("BRAND_ID", f'"{brand_id}"')


# ── Supervisor factory ─────────────────────────────────────────────────────────

async def _build_supervisor(brand_id: str, brand_name: str):

    store        = await _get_store()
    checkpointer = await _get_checkpointer()

    # Seed brand memory into store if first time
    await _ensure_brand_seeded(brand_id, brand_name, store)

    # ── Shopify client — shared by inventory + pricing subagents ───────────────
    shopify_client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    shopify_tools = await shopify_client.get_tools()

    inventory_subagent = await build_inventory_subagent(shopify_tools)
    pricing_subagent   = await build_pricing_subagent(shopify_tools)

    # ── Social + Trends client — for trend subagent ────────────────────────────
    trend_client = MultiServerMCPClient({
        "social": {"url": SOCIAL_MCP_URL, "transport": "http"},
        "trends": {"url": TRENDS_MCP_URL, "transport": "http"},
    })
    trend_tools    = await trend_client.get_tools()
    trend_subagent = await build_trend_subagent(trend_tools)

    # ── Ads client — for marketing subagent ───────────────────────────────────
    ads_client = MultiServerMCPClient(
        {"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}}
    )
    ads_tools          = await ads_client.get_tools()
    marketing_subagent = await build_marketing_subagent(ads_tools)

    content_subagent = await build_content_subagent([])

    # ── Backend ────────────────────────────────────────────────────────────────
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda rt: (brand_id,),
            ),
            "/skills/": FilesystemBackend(
                root_dir=str(SKILLS_DIR),
                virtual_mode=True,
            ),
        },
    )

    agent = create_deep_agent(
        name          = f"fashionos-{brand_id}",
        # model         = "google_genai:gemini-2.5-flash-lite",
        model         = chat_model,
        system_prompt = _build_prompt(brand_id, brand_name),
        tools         = get_db_tools(),
        subagents     = [
            inventory_subagent,
            trend_subagent,
            pricing_subagent,
            marketing_subagent,      
            content_subagent,  
        ],
        backend       = backend,
        store         = store,                    #  the actual persistent store -- Redis store
        memory        = ["/memories/AGENTS.md"],  #  long-term, loaded at startup
        skills        = ["/skills/"],
        checkpointer  = checkpointer,
        permissions   = [
            FilesystemPermission(
                operations=["write", "edit"],
                paths=["/skills/**"],
                mode="deny",
            ),
        ],
    )
    return agent


async def _get_cached_agent(brand_id: str, brand_name: str):
    if brand_id not in _agent_cache:
        agent = await _build_supervisor(brand_id, brand_name)
        _agent_cache[brand_id] = agent
        print(f"[Supervisor] ✓ Built + cached agent for brand={brand_id}")
    return _agent_cache[brand_id]


# ── Public chat interface ──────────────────────────────────────────────────────

async def chat(
    brand_id:   str,
    brand_name: str,
    message:    str,
    thread_id:  str = "default",
) -> str:
    agent = await _get_cached_agent(brand_id, brand_name)

    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
    )
    msgs = result.get("messages", [])
    if msgs:
        last = msgs[-1]
        return getattr(last, "content", str(last))
    return "No response generated."


# ── CLI ────────────────────────────────────────────────────────────────────────

async def _cli():
    import sys, uuid

    brand_id   = "bra_2"
    brand_name = "bra_3"

    if len(sys.argv) >= 3:
        brand_id, brand_name = sys.argv[1], sys.argv[2]

    session_id = "cli_session"

    print(f"\n{'═' * 60}")
    print(f"  FashionOS Supervisor — {brand_name} ({brand_id})")
    print(f"  Session: {brand_id}:{session_id}")
    print(f"  Type 'quit' to exit | 'reset' to start new session")
    print(f"{'═' * 60}\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if user_input.lower() == "reset":
            session_id = str(uuid.uuid4())[:8]
            print(f"[Session reset → {session_id}]\n")
            continue
        if not user_input:
            continue

        response = await chat(brand_id, brand_name, user_input, thread_id=session_id)
        print(f"\nFashionOS: {response}\n")


if __name__ == "__main__":
    asyncio.run(_cli())