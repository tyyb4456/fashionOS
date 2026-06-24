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
from deep_agents.subagents.trend_agent import build_trend_subagent
from deep_agents.subagents.pricing_agent import build_pricing_subagent


from deep_agents.subagents.inventory_agent import build_inventory_subagent
from deep_agents.tools.db_tools import get_db_tools

load_dotenv()

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
SOCIAL_MCP_URL  = os.getenv("SOCIAL_MCP_URL",  "http://localhost:8002/mcp")
TRENDS_MCP_URL  = os.getenv("TRENDS_MCP_URL",  "http://localhost:8003/mcp")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
BASE_DIR        = Path(__file__).parent.resolve()
SKILLS_DIR      = BASE_DIR / "skills"

# ── Redis store — one instance for whole process ───────────────────────────────
# ADD lazy init:
_store: AsyncRedisStore | None = None

async def _get_store() -> AsyncRedisStore:
    global _store
    if _store is None:
        _store = AsyncRedisStore(redis_url=REDIS_URL)
        await _store.setup()          # setup once, here
    return _store



# NEW — lazy checkpointer (same pattern as _get_store)
_checkpointer: AsyncRedisSaver | None = None

async def _get_checkpointer() -> AsyncRedisSaver:
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = AsyncRedisSaver(redis_url=REDIS_URL)
        await _checkpointer.asetup()   # creates Redis indices once
        print("[Checkpointer] ✓ AsyncRedisSaver ready")
    return _checkpointer

# ── Per-brand agent cache ──────────────────────────────────────────────────────
# key: brand_id → agent
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
        # Item object — access .value, not .get()
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

## Output format
✘ CRITICAL  (action needed today)
⚠ WARNING   (action needed this week)
✔ HEALTHY    (no action needed)

Always include real numbers (stock, velocity, PKR, days).

## Hard rules
1. Never call Shopify or Meta APIs directly — delegate to subagents.
2. Never guess at numbers — always call a tool first.
3. /memories/AGENTS.md overrides all global defaults for this brand.
4. Never write to /skills/ — read-only.
5. Always pass brand_id=BRAND_ID to every DB tool call.
6. When updating /memories/AGENTS.md, ALWAYS read it first to get exact line content.
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

    store = await _get_store()  
    checkpointer = await _get_checkpointer()

    # Seed brand memory into store if first time
    await _ensure_brand_seeded(brand_id, brand_name, store)

    shopify_client    = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    shopify_tools = await shopify_client.get_tools()
    inventory_subagent = await build_inventory_subagent(shopify_tools)

    trend_client = MultiServerMCPClient({
        "social": {"url": SOCIAL_MCP_URL, "transport": "http"},
        "trends": {"url": TRENDS_MCP_URL, "transport": "http"},
    })
    trend_tools = await trend_client.get_tools()
    trend_subagent = await build_trend_subagent(trend_tools)

    pricing_subagent = await build_pricing_subagent(shopify_tools)

    backend = CompositeBackend(
        default=StateBackend(),

        routes={
            # LONG-TERM memory → StoreBackend (persists per brand)  VIRTUAL — lives in Redis, keyed by brand_id
            "/memories/": StoreBackend(
                namespace=lambda rt: (brand_id,),
            ),
            # SKILLS → FilesystemBackend (static files on disk, read-only)
            "/skills/": FilesystemBackend(
                root_dir=str(SKILLS_DIR),
                virtual_mode=True,   # ← explicit, silences the warning
            ),
        },
    )

    agent = create_deep_agent(
        name          = f"fashionos-{brand_id}",
        model         = "google_genai:gemini-2.5-flash-lite",
        system_prompt = _build_prompt(brand_id, brand_name),
        tools         = get_db_tools(),
        subagents     = [inventory_subagent, trend_subagent, pricing_subagent],
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

    # brand_id prefix = cross-tenant isolation
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
    # brand_id   = os.getenv("BRAND_ID",   "brand_user_3fe1ek8")
    # brand_name = os.getenv("BRAND_NAME", "Dev Brand")
    brand_id = "bra_2"
    brand_name = "bra_3"

    if len(sys.argv) >= 3:
        brand_id, brand_name = sys.argv[1], sys.argv[2]

    session_id = "cli_session"

    print(f"\n{'═' * 60}")
    print(f"  FashionOS Supervisor — {brand_name} ({brand_id})")
    print(f"  Store  : InMemoryStore (swap → RedisStore in prod)")
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