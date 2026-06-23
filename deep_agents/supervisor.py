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


from deep_agents.subagents.inventory_agent import build_inventory_subagent
from deep_agents.tools.db_tools import get_db_tools

load_dotenv()

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
BASE_DIR        = Path(__file__).parent.resolve()
SKILLS_DIR      = BASE_DIR / "skills"

# ── Redis store — one instance for whole process ───────────────────────────────
store = AsyncRedisStore(redis_url=REDIS_URL)


# ── Per-brand agent cache ──────────────────────────────────────────────────────
# key: brand_id → agent
_agent_cache: dict[str, object] = {}


# ── Brand memory seeding ───────────────────────────────────────────────────────

def _seed_agents_md(brand_id: str, brand_name: str) -> str:
    return f"""# FashionOS Brand Memory — {brand_name}

## Brand Identity
- brand_id  : {brand_id}
- brand_name: {brand_name}
- Platform  : Shopify + Meta Ads + Instagram
- Currency  : PKR (Pakistani Rupee)
- Market    : Pakistani fashion e-commerce

## Founder Preferences
<!-- Agent updates this as it learns. Examples:
- Prefers bundles over clearance codes for dead stock
- Wants bullet-point summaries only
- Critical alerts → WhatsApp only -->

## Brand-Specific Rules
<!-- Overrides of global FashionOS defaults:
- Min margin floor: 38%
- Never increase ad budget on Fridays
- Price endings: always PKR X99 or X499 -->

## Supplier Notes
<!-- Agent records supplier facts:
- Primary: Ahmed at Shadman Market, 5-day lead -->

## Seasonal Patterns Observed
<!-- Agent records patterns over time:
- Eid run-up (2 weeks before): velocity 3x normal -->

## Past Decisions Log
<!-- Agent logs major decisions to prevent repeating bad ones -->
"""


async def _ensure_brand_seeded(brand_id: str, brand_name: str) -> None:
    """
    Seeds /memories/AGENTS.md into Redis for a brand if not already there.
    Key in Redis: namespace=(brand_id,), key="/memories/AGENTS.md"
    """
    namespace = (brand_id,)
    key       = "/memories/AGENTS.md"

    existing = await store.aget(namespace, key)   # async get
    if existing is None:
        await store.aput(                          # async put
            namespace,
            key,
            create_file_data(_seed_agents_md(brand_id, brand_name)),
        )
        print(f"[Memory] ✓ Seeded AGENTS.md for brand={brand_id} in Redis")


# ── System prompt ──────────────────────────────────────────────────────────────

_PROMPT_BASE = """\
You are FashionOS Supervisor — the autonomous AI brain of a Pakistani Shopify fashion brand.

## Memory

### Long-term (persists across ALL conversations)
/memories/AGENTS.md is injected at startup. Contains brand rules, preferences,
supplier contacts, seasonal patterns, and past decisions.
UPDATE when you learn something new:
  edit_file("/memories/AGENTS.md", old_text, new_text)

### Short-term (this conversation only)
Conversation history is automatic — no action needed.

### Operational data
DB tools give you latest pipeline results (inventory, alerts, pricing, content).

## Tool strategy
- Existing data (fast)  → DB tools
- Fresh live analysis   → delegate to subagents
- Learn something new   → edit_file("/memories/AGENTS.md", ...)

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

    await store.setup()

    # Seed brand memory into store if first time
    _ensure_brand_seeded(brand_id, brand_name)

    client    = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    mcp_tools = await client.get_tools()

    inventory_subagent = await build_inventory_subagent(mcp_tools)

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
            ),
        },
    )

    agent = create_deep_agent(
        name          = f"fashionos-{brand_id}",
        model         = "google_genai:gemini-2.5-flash-lite",
        system_prompt = _build_prompt(brand_id, brand_name),
        tools         = get_db_tools(),
        subagents     = [inventory_subagent],
        backend       = backend,
        store         = store,                    #  the actual persistent store -- Redis store
        memory        = ["/memories/AGENTS.md"],  #  long-term, loaded at startup
        skills        = ["/skills/"],
        #  NO checkpointer — short-term is automatic via thread_id
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
    brand_id   = os.getenv("BRAND_ID",   "brand_dev")
    brand_name = os.getenv("BRAND_NAME", "Dev Brand")

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