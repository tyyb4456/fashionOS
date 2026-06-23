"""
FashionOS Deep Agent Supervisor — Multi-Tenant
================================================
Memory layers:

SHORT-TERM (within a conversation):
  MemorySaver (LangGraph) keyed by thread_id = f"{brand_id}:{session_id}"
  Server-side. Frontend only sends the new message, not full history.
  Scoped per brand per session — no cross-tenant bleed.
  Resets on server restart (acceptable — conversations are short operational sessions).
  Upgrade path: swap MemorySaver → AsyncPostgresCheckpointer for full persistence.

LONG-TERM (across conversations):
  /memories/AGENTS.md → FilesystemBackend(root_dir=memory/{brand_id}/)
  Persists indefinitely on disk (mounted Docker volume in prod).
  Agent edits this file as it learns brand-specific preferences and patterns.
  Survives server restarts. Completely isolated per brand.

EPHEMERAL (within a step):
  /workspace/ → StateBackend()
  Scratch space for multi-step analysis. Gone after the conversation.
"""

import os
import asyncio
from pathlib import Path
from typing import Optional

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langchain_mcp_adapters.client import MultiServerMCPClient

from deep_agents.subagents.inventory_agent import build_inventory_subagent
from deep_agents.tools.db_tools import get_db_tools

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
BASE_DIR        = Path(__file__).parent.resolve()
MEMORY_DIR      = BASE_DIR / "memory"
SKILLS_DIR      = BASE_DIR / "skills"

# ── Per-brand agent + checkpointer cache ───────────────────────────────────────
# Keeps one MemorySaver per brand alive for the process lifetime.
# Short-term conversation memory survives multiple requests to the same process.
# Key: brand_id → (compiled_agent, MemorySaver)
_agent_cache: dict[str, tuple] = {}


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
<!-- Agent updates this as it learns. Examples:                          -->
<!-- - Prefers bundles over clearance codes for dead stock               -->
<!-- - Wants bullet-point summaries only, no long paragraphs             -->
<!-- - Critical alerts → WhatsApp only, not email                        -->
<!-- - Reviews dashboard at 9 PM PKT — send notifications before then   -->

## Brand-Specific Rules
<!-- Overrides of global FashionOS defaults:                             -->
<!-- - Min margin floor: 38% (stricter than global 35%)                  -->
<!-- - Never increase ad budget on Fridays — historically bad ROAS       -->
<!-- - Price endings: always PKR X99 or X499                             -->

## Supplier Notes
<!-- Agent records supplier facts learned from founder:                  -->
<!-- - Primary: Ahmed at Shadman Market Lahore +923XXXXXXXXX, 5-day lead -->
<!-- - Backup: Faisalabad textile hub for bulk knitwear                  -->

## Seasonal Patterns Observed
<!-- Agent records patterns from pipeline data over time:                -->
<!-- - Eid run-up (2 weeks before): velocity 3× normal — pre-stock hard  -->
<!-- - Peak IG engagement: 8–10 PM PKT — schedule content here          -->

## Past Decisions Log
<!-- Agent logs major decisions approved or rejected by founder.         -->
<!-- Prevents repeating bad decisions and builds on what worked.         -->
"""


def _ensure_brand_memory(brand_id: str, brand_name: str) -> Path:
    brand_dir = MEMORY_DIR / brand_id
    brand_dir.mkdir(parents=True, exist_ok=True)
    agents_md = brand_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_seed_agents_md(brand_id, brand_name), encoding="utf-8")
        print(f"[Memory] ✓ Seeded AGENTS.md for brand={brand_id} ({brand_name})")
    return brand_dir


# ── System prompt ──────────────────────────────────────────────────────────────

_PROMPT_BASE = """\
You are FashionOS Supervisor — the autonomous AI brain of a Pakistani Shopify fashion brand.

## Memory layers

### Long-term memory (persists across ALL conversations)
/memories/AGENTS.md is always injected at startup. It contains:
- Brand-specific rules that override global FashionOS defaults
- Founder preferences learned over time  
- Supplier contacts and lead times specific to this brand
- Seasonal patterns observed from pipeline history

UPDATE THIS FILE when you learn something new. Use:
  edit_file("/memories/AGENTS.md", old_text, new_text)
This is how the agent gets smarter with each conversation.

### Short-term memory (this conversation only)
LangGraph checkpointer automatically maintains conversation context within this session.
You don't need to ask for context you already established earlier in this conversation.

### Operational data (structured DB)
DB tools give you the latest pipeline results — inventory, alerts, pricing decisions, etc.

## Tool strategy

### Questions about existing data (fast — DB tools):
- Inventory status       → get_inventory_status(brand_id=BRAND_ID)
- Pending approvals      → get_pending_approvals(brand_id=BRAND_ID)
- Open alerts            → get_open_alerts(brand_id=BRAND_ID)
- Recent activity        → get_run_history(brand_id=BRAND_ID)
- Specific SKU           → get_sku_history(brand_id=BRAND_ID, sku="...")
- Last pipeline run      → get_pipeline_status(brand_id=BRAND_ID)
- Content to post        → get_content_queue(brand_id=BRAND_ID)
- Return patterns        → get_return_insights(brand_id=BRAND_ID)

### Live fresh analysis (slow — subagents):
- Fresh inventory check  → task(name="inventory-agent", ...)

### Memory operations:
- Learn new preference   → edit_file("/memories/AGENTS.md", ...) then confirm to founder
- Check brand rules      → already in context at startup, no need to read again

## Output format
🔴 CRITICAL  (action needed today)
🟡 WARNING   (action needed this week)
🟢 HEALTHY   (no action needed)

One bullet per SKU or decision. Always include real numbers (stock, velocity, PKR, days).
End with "X items pending your approval in the dashboard." when approvals exist.

## Hard rules
1. Never call Shopify or Meta APIs directly — delegate to subagents.
2. Never guess at numbers — always call a tool first.
3. /memories/AGENTS.md overrides all global defaults for this brand.
4. Never write to /skills/ — those are read-only system definitions.
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

async def _build_supervisor(brand_id: str, brand_name: str, checkpointer: MemorySaver):
    """
    Builds a brand-scoped deep agent with:
    - MemorySaver checkpointer for short-term conversation memory
    - CompositeBackend for long-term (AGENTS.md) + skills + ephemeral workspace
    """
    brand_dir = _ensure_brand_memory(brand_id, brand_name)

    client    = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    mcp_tools = await client.get_tools()

    inventory_subagent = await build_inventory_subagent(mcp_tools)

    backend = CompositeBackend(
        # /workspace/ and anything else → ephemeral scratch per conversation
        default=StateBackend(),
        routes={
            # Long-term writable brand memory
            "/memories/": FilesystemBackend(
                root_dir=str(brand_dir),
                virtual_mode=True,
            ),
            # Read-only shared system skills
            "/skills/": FilesystemBackend(
                root_dir=str(SKILLS_DIR),
                virtual_mode=True,
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
        checkpointer  = checkpointer,          # ← short-term memory
        memory        = ["/memories/AGENTS.md"],  # ← long-term memory injected at startup
        skills        = ["/skills/"],
        permissions   = [
            FilesystemPermission(
                operations=["write", "edit"],
                paths=["/skills/**"],
                mode="deny",
            ),
        ],
    )
    return agent


async def _get_cached_agent(brand_id: str, brand_name: str) -> tuple:
    """
    Returns (agent, checkpointer) from cache, building if first time.
    Cache key = brand_id. One MemorySaver per brand per process lifetime.

    Thread-safety note: asyncio is single-threaded so no lock needed here.
    If you move to multi-worker Gunicorn, switch to AsyncPostgresCheckpointer.
    """
    if brand_id not in _agent_cache:
        checkpointer = MemorySaver()
        agent        = await _build_supervisor(brand_id, brand_name, checkpointer)
        _agent_cache[brand_id] = (agent, checkpointer)
        print(f"[Supervisor] ✓ Built + cached agent for brand={brand_id}")
    return _agent_cache[brand_id]


# ── Public chat interface ──────────────────────────────────────────────────────

async def chat(
    brand_id:   str,
    brand_name: str,
    message:    str,
    thread_id:  str = "default",
) -> str:
    """
    Single-turn or multi-turn conversational query for a specific brand.

    Short-term memory is handled server-side via MemorySaver + thread_id.
    The frontend sends only the new message — no history list needed.

    thread_id scoping:
      thread_id = session_id from the frontend (e.g. a UUID per browser tab).
      Full LangGraph key = f"{brand_id}:{thread_id}" — prevents cross-tenant bleed.

    Long-term memory (AGENTS.md) is automatically injected at startup from disk.
    """
    agent, _ = await _get_cached_agent(brand_id, brand_name)

    # Namespace thread_id by brand_id — critical for multi-tenant isolation
    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
    )
    msgs = result.get("messages", [])
    if msgs:
        last = msgs[-1]
        return getattr(last, "content", str(last))
    return "No response generated."


# ── CLI for local testing ──────────────────────────────────────────────────────

async def _cli():
    import sys
    brand_id   = os.getenv("BRAND_ID",   "brand_dev")
    brand_name = os.getenv("BRAND_NAME", "Dev Brand")

    if len(sys.argv) >= 3:
        brand_id, brand_name = sys.argv[1], sys.argv[2]

    session_id = "cli_session"

    print(f"\n{'═' * 60}")
    print(f"  FashionOS Supervisor — {brand_name} ({brand_id})")
    print(f"  Memory : {MEMORY_DIR / brand_id / 'AGENTS.md'}")
    print(f"  Session: {brand_id}:{session_id}")
    print(f"  Type 'quit' to exit | 'reset' to start new session")
    print(f"{'═' * 60}\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if user_input.lower() == "reset":
            import uuid
            session_id = str(uuid.uuid4())[:8]
            print(f"[Session reset → {session_id}]\n")
            continue
        if not user_input:
            continue

        response = await chat(brand_id, brand_name, user_input, thread_id=session_id)
        print(f"\nFashionOS: {response}\n")


if __name__ == "__main__":
    asyncio.run(_cli())