"""
FashionOS Deep Agent Supervisor — Multi-Tenant
================================================
SHORT-TERM:  Automatic via Deep Agents + thread_id. No MemorySaver needed.
             thread_id = f"{brand_id}:{session_id}" — cross-tenant isolation.

LONG-TERM:   StoreBackend keyed by brand_id namespace.
             /memories/AGENTS.md → persists across ALL conversations per brand.
             Agent edits this file as it learns. Seeded on first brand creation.

EPHEMERAL:   StateBackend() for /workspace/ scratch. Gone after conversation.

================================================
Updated with streaming support via astream().

stream_chat() yields SSE-ready dicts:
  {"type": "token",          "content": "..."}
  {"type": "subagent_start", "name": "inventory-agent"}
  {"type": "subagent_token", "name": "inventory-agent", "content": "..."}
  {"type": "subagent_done",  "name": "inventory-agent", "summary": "..."}
  {"type": "done"}

"""

from pydantic import BaseModel as _PydanticBase

# Patch 1 — Redis serializer (serialization: writing Pydantic → checkpoint)
try:
    from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
    _orig = JsonPlusRedisSerializer._default_handler
    def _patched(self, obj):
        if isinstance(obj, _PydanticBase):
            return obj.model_dump()
        return _orig(self, obj)
    JsonPlusRedisSerializer._default_handler = _patched
    print("[FashionOS] ✓ JsonPlusRedisSerializer patched")
except Exception as e:
    print(f"[FashionOS] ⚠ Redis serializer patch failed: {e}")

# Official LangGraph fix for custom Pydantic types in checkpoints
# Format: [(module_path, classname), ...]  — exactly as shown in the warning message
FASHIONOS_ALLOWED_MSGPACK = [
    ("response_schemas.inventory_model", "InventoryAnalysis"),
    ("response_schemas.inventory_model", "SnapshotOut"),
    ("response_schemas.inventory_model", "AlertOut"),
    ("response_schemas.trend_model",     "TrendAnalysis"),
    ("response_schemas.trend_model",     "TrendSignalOut"),
    ("response_schemas.trend_model",     "TrendAlertOut"),
    ("response_schemas.pricing_model",   "PricingAnalysis"),
    ("response_schemas.pricing_model",   "PricingDecisionOut"),
    ("response_schemas.marketing_model", "MarketingAnalysis"),
    ("response_schemas.marketing_model", "CampaignDecisionOut"),
    ("response_schemas.restock_model",   "RestockAnalysis"),
    ("response_schemas.restock_model",   "RestockDecisionOut"),
    ("response_schemas.restock_model",   "SupplierBatch"),
    ("response_schemas.content_model",   "ContentPlan"),
    ("response_schemas.content_model",   "ContentPostOut"),
    ("response_schemas.content_model",   "ContentFatigueSkip"),
    ("response_schemas.content_model",   "InstagramOut"),
    ("response_schemas.content_model",   "TikTokOut"),
    ("response_schemas.content_model",   "ShotListItem"),
]

import os
import asyncio
from pathlib import Path
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pydantic import BaseModel as _PydanticBase

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
from deep_agents.subagents.dm_agent import build_dm_subagent

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

# Known subagent node names (must match the `name` field in each subagent dict)
SUBAGENT_NAMES = {
    "inventory-agent",
    "trend-agent",
    "pricing-agent",
    "marketing-agent",
    "content-agent",
}

# Redis namespace prefix for conversation metadata (separate from brand memory)
_CONVOS_NS = "convos"


# ── Singletons ────────────────────────────────────────────────────────────────

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
        # Patch the allowlist onto the existing internal serde instead of replacing it.
        # Replacing it breaks AsyncRedisSaver's own serde subclass (_preprocess_interrupts etc.)
        _checkpointer.serde.allowed_msgpack_modules = FASHIONOS_ALLOWED_MSGPACK
        await _checkpointer.asetup()
        print("[Checkpointer] ✓ AsyncRedisSaver ready")
    return _checkpointer

_agent_cache: dict[str, object] = {}

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

    existing = await store.aget(namespace, key)
    if existing is None:
        await store.aput(namespace, key, create_file_data(_seed_agents_md(brand_id, brand_name)))
        print(f"[Memory] ✓ Seeded AGENTS.md for brand={brand_id}")
    else:
        print(f"[Memory] ✓ AGENTS.md already exists for brand={brand_id}, skipping seed")


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

  dm-agent:
  # Runs on its own 30-minute schedule — independent of daily pipeline.
  # Call conversationally when founder asks about DMs, complaints, or bulk orders.
  # Optional: pass inventory_snapshot for accurate availability answers.
  # Optional: pass return_insights for product-specific return policy replies.
  task(
      name="dm-agent",
      task=(
          "Process Instagram DMs for {brand_name} (brand_id={brand_id}). "
          "inventory_snapshot: {inventory_json} "
          "return_insights: {return_insights_json} "
          "Fetch DMs, classify, auto-reply where safe, flag the rest, return DmAnalysis."
      )
  )
  → auto_sends: size_question, availability, order_status, general_inquiry, pricing_inquiry
  → flags with draft: bulk_inquiry (revenue), complaint (churn), influencer (collab)
  → batch_stats.action_items: systemic fixes from pattern analysis (e.g. update size guide)
  → critical_flags: conversation_ids needing IMMEDIATE founder response
 
  # How to build enrichment context:
  inventory_json      = json.dumps(get_inventory_status(brand_id).get("skus", []))
  return_insights_json = json.dumps(get_return_insights(brand_id) or [])
 
  # You can also call dm-agent with minimal context:
  task(name="dm-agent", task="Process Instagram DMs for {brand_name} (brand_id={brand_id}).")
  # It will default to generic availability replies (reply_confidence=medium).


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

from deep_agents.load_model import llm

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

    dm_subagent = await build_dm_subagent(trend_tools)   # social-mcp tools already in here

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
        model         = llm,
        # model         = chat_model,
        system_prompt = _build_prompt(brand_id, brand_name),
        tools         = get_db_tools(),
        subagents     = [
            inventory_subagent,
            trend_subagent,
            pricing_subagent,
            marketing_subagent,      
            content_subagent,  
            dm_subagent,  
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


# ── Conversation metadata helpers (stored in Redis via AsyncRedisStore) ────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def save_conversation_meta(
    brand_id:  str,
    thread_id: str,
    title:     str,
) -> None:
    """
    Upsert conversation metadata in Redis.
    Namespace: (_CONVOS_NS, brand_id)   Key: thread_id
    Preserves created_at on updates.
    """
    store     = await _get_store()
    namespace = (_CONVOS_NS, brand_id)
    now       = _now_iso()

    existing   = await store.aget(namespace, thread_id)
    created_at = (existing.value or {}).get("created_at", now) if existing else now

    await store.aput(namespace, thread_id, {
        "thread_id":  thread_id,
        "title":      title[:80],
        "created_at": created_at,
        "updated_at": now,
    })


async def list_conversations(brand_id: str) -> list[dict]:
    """
    Return all conversations for a brand, sorted newest-first by updated_at.
    Filters out soft-deleted records and any items missing required fields.
    """
    store     = await _get_store()
    namespace = (_CONVOS_NS, brand_id)
    items     = await store.asearch(namespace, limit=200)

    _REQUIRED = {"thread_id", "title", "created_at", "updated_at"}
    convos = [
        item.value for item in items
        if item.value
        and not item.value.get("_deleted")
        and _REQUIRED.issubset(item.value.keys())
    ]
    return sorted(convos, key=lambda x: x.get("updated_at", ""), reverse=True)


async def delete_conversation(brand_id: str, thread_id: str) -> None:
    """
    Remove conversation metadata from Redis.

    Tries hard-delete via store.adelete first.
    Falls back to a soft-delete marker so list_conversations() filters it out —
    this covers LangGraph versions where adelete may not be implemented.
    """
    store     = await _get_store()
    namespace = (_CONVOS_NS, brand_id)

    deleted = False
    try:
        await store.adelete(namespace, thread_id)
        deleted = True
        print(f"[Convos] hard-deleted {thread_id} for brand={brand_id}")
    except Exception as exc:
        print(f"[Convos] adelete unavailable ({exc}), using soft-delete marker")

    if not deleted:
        # Soft-delete: mark the record; list_conversations filters these out
        try:
            await store.aput(namespace, thread_id, {"_deleted": True})
        except Exception as exc2:
            print(f"[Convos] soft-delete also failed: {exc2}")
            raise


async def _extract_messages(raw_messages: list) -> list[dict]:
    """
    Shared helper: turn a list of LangChain message objects or raw dicts
    into [{role, content}] — human + ai only.
    """
    result: list[dict] = []
    for msg in raw_messages:
        # LangChain objects expose .type; raw dicts from Redis use "type" key
        if isinstance(msg, dict):
            msg_type = msg.get("type", "")
            content  = msg.get("content", "")
        else:
            # Proper LangChain objects: HumanMessage.type="human", AIMessage.type="ai"
            msg_type = getattr(msg, "type", "") or ""
            if not msg_type:
                # Older versions may not have .type — fall back to class name
                cls      = msg.__class__.__name__
                msg_type = "human" if "Human" in cls else ("ai" if "AI" in cls else "other")
            content = getattr(msg, "content", "") or ""

        if msg_type not in ("human", "ai"):
            continue  # skip tool, system, function messages

        # Flatten multi-modal / list content to plain text
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )

        result.append({
            "role":    "user" if msg_type == "human" else "assistant",
            "content": str(content).strip(),
        })
    return result


async def get_thread_messages(
    brand_id:   str,
    brand_name: str,
    thread_id:  str,
) -> list[dict]:
    """
    Replay the human + AI messages for a thread.

    Strategy (most reliable first):
      1. agent.aget_state()  — fully deserialized LangChain message objects
      2. checkpointer.aget_tuple() — raw checkpoint, channel_values.messages
    Both fall back gracefully and return [] on failure.
    """
    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    # ── Primary: use the compiled graph's aget_state ──────────────────────────
    try:
        agent        = await _get_cached_agent(brand_id, brand_name)
        state        = await agent.aget_state(config)
        raw_messages = (state.values or {}).get("messages", []) if state else []
        if raw_messages:
            result = await _extract_messages(raw_messages)
            print(f"[Convos] aget_state returned {len(result)} messages for {thread_id}")
            return result
        print(f"[Convos] aget_state returned 0 messages for {thread_id}, trying checkpointer")
    except Exception as exc:
        print(f"[Convos] aget_state failed ({exc}), falling back to checkpointer")

    # ── Fallback: read checkpoint directly ─────────────────────────────────────
    try:
        checkpointer = await _get_checkpointer()
        cp_tuple     = await checkpointer.aget_tuple(config)
        if not cp_tuple:
            print(f"[Convos] no checkpoint found for {thread_id}")
            return []

        checkpoint   = cp_tuple.checkpoint or {}
        # LangGraph stores graph state in channel_values
        raw_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
        result = await _extract_messages(raw_messages)
        print(f"[Convos] checkpoint fallback returned {len(result)} messages for {thread_id}")
        return result
    except Exception as exc:
        print(f"[Convos] checkpoint fallback failed: {exc}")
        return []


# ── Public chat — non-streaming ───────────────────────────────────────────────

async def chat(
    brand_id:   str,
    brand_name: str,
    message:    str,
    thread_id:  str = "default",
) -> str:
    agent         = await _get_cached_agent(brand_id, brand_name)
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


# ── Subagent result persistence ───────────────────────────────────────────────

async def _save_subagent_result(
    brand_id:   str,
    thread_id:  str,
    turn_index: int,
    agent_name: str,
    summary:    str,
    data:       dict,
) -> None:
    """Fire-and-forget: persist one subagent result row to PostgreSQL."""
    from db.session import AsyncSessionLocal
    from db.models  import ChatSubagentResult
    try:
        async with AsyncSessionLocal() as session:
            session.add(ChatSubagentResult(
                brand_id=brand_id,
                thread_id=thread_id,
                turn_index=turn_index,
                agent_name=agent_name,
                summary=summary,
                data=data,
            ))
            await session.commit()
    except Exception as exc:
        print(f"[Supervisor] ⚠ Failed to persist subagent result ({agent_name}): {exc}")

# ── Public chat — streaming ───────────────────────────────────────────────────

async def stream_chat(
    brand_id:   str,
    brand_name: str,
    message:    str,
    thread_id:  str = "default",
) -> AsyncGenerator[dict, None]:
    """
    Yields SSE-ready event dicts. See module docstring for event types.
    """
    agent         = await _get_cached_agent(brand_id, brand_name)
    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    # Count existing AI messages to determine which turn_index this stream is.
    # This lets us re-attach subagent cards to the right assistant message later.
    try:
        state         = await agent.aget_state(config)
        existing_msgs = (state.values or {}).get("messages", []) or []
        turn_index    = sum(1 for m in existing_msgs if getattr(m, "type", "") == "ai")
    except Exception:
        turn_index = 0

    active_subagents: set[str]  = set()
    subagent_buffers: dict[str, str] = {}

    # Maps tool_call_id → subagent_name so we can intercept ToolMessage results
    _tc_id_to_agent: dict[str, str] = {}
    async def _flush_subagent_buffer(sa: str) -> dict | None:
        """Parse buffer; if complete JSON with summary, persist to DB and return event."""
        buf = subagent_buffers.get(sa, "")
        if not buf:
            return None
        try:
            parsed  = json.loads(buf)
            summary = (
                parsed.get("summary")
                or parsed.get("analysis_summary")
                or parsed.get("run_summary")
                or ""
            )
            if summary:
                subagent_buffers[sa] = ""
                # Persist to PostgreSQL (fire-and-forget — never blocks streaming)
                asyncio.ensure_future(_save_subagent_result(
                    brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
                    agent_name=sa, summary=summary, data=parsed,
                ))
                return {"type": "subagent_done", "name": sa, "summary": summary, "data": parsed}
        except (json.JSONDecodeError, AttributeError):
            pass
        return None
    def _buffer_subagent_text(sa: str, text: str):
        subagent_buffers[sa] = subagent_buffers.get(sa, "") + text

    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
        ):
            if not isinstance(chunk, tuple) or len(chunk) != 2:
                continue

            msg_chunk, metadata = chunk
            node: str = metadata.get("langgraph_node", "") or ""

            raw = getattr(msg_chunk, "content", "") or ""

            # Subagent structured outputs may arrive as Pydantic model objects.
            # Convert to JSON string so the buffer can be parsed downstream.
            if isinstance(raw, _PydanticBase):
                raw = raw.model_dump_json()

            text = (
                "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in raw)
                if isinstance(raw, list) else str(raw)
            )

            # ── Detect task-tool delegation ────────────────────────────────
            for tc in (getattr(msg_chunk, "tool_calls", []) or []):
                if tc.get("name") == "task":
                    args    = tc.get("args") or {}
                    sa_name = args.get("name") or args.get("subagent_type") or ""
                    tc_id   = tc.get("id", "")
                    if sa_name:
                        if sa_name not in active_subagents:
                            active_subagents.add(sa_name)
                            subagent_buffers[sa_name] = ""
                            yield {"type": "subagent_start", "name": sa_name}
                        if tc_id:
                            _tc_id_to_agent[tc_id] = sa_name
            # ── 2. Intercept ToolMessage results — route to subagent buffer ───────
            #    ToolMessage has tool_call_id; this is where the raw JSON arrives.
            tc_id = getattr(msg_chunk, "tool_call_id", None)
            if tc_id and tc_id in _tc_id_to_agent:
                sa_name = _tc_id_to_agent[tc_id]
                if text:
                    _buffer_subagent_text(sa_name, text)
                    event = await _flush_subagent_buffer(sa_name)
                    if event:
                        yield event
                continue   # never emit tool results as tokens
            # ── 3. Route by node name (when subagent runs as its own graph node) ──

            # ── Route by node ──────────────────────────────────────────────
            if node in SUBAGENT_NAMES:
                if node not in active_subagents:
                    active_subagents.add(node)
                    subagent_buffers[node] = ""
                    yield {"type": "subagent_start", "name": node}

                if text:
                    _buffer_subagent_text(node, text)
                    event = await _flush_subagent_buffer(node)
                    if event:
                        yield event

            # ── 4. Anything else with text → regular supervisor token ─────────────
            elif text:

                stripped = text.strip()
                if stripped.startswith("{") and (
                    '"inventory_snapshots"' in stripped
                    or '"trend_signals"'     in stripped
                    or '"decisions"'         in stripped
                    or '"posts"'             in stripped
                ):
              
                    for sa in list(subagent_buffers):
                        _buffer_subagent_text(sa, text)
                        event = await _flush_subagent_buffer(sa)
                        if event:
                            yield event
                            break
                else:
                    yield {"type": "token", "content": text}

    except Exception as exc:
        yield {"type": "error", "content": str(exc)}

    # Flush any remaining subagent buffers and persist to DB
    for sa_name, buf in subagent_buffers.items():
        if buf:
            try:
                parsed  = json.loads(buf)
                summary = parsed.get("summary") or parsed.get("analysis_summary") or ""
                asyncio.ensure_future(_save_subagent_result(
                    brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
                    agent_name=sa_name, summary=summary or "", data=parsed,
                ))
                yield {
                    "type":    "subagent_done",
                    "name":    sa_name,
                    "summary": summary or buf[:300] + ("…" if len(buf) > 300 else ""),
                    "data":    parsed,
                }
            except (json.JSONDecodeError, AttributeError):
                yield {
                    "type":    "subagent_done",
                    "name":    sa_name,
                    "summary": buf[:300] + ("…" if len(buf) > 300 else ""),
                    "data":    None,
                }

    yield {"type": "done"}


# ── CLI ────────────────────────────────────────────────────────────────────────

async def _cli():
    import sys, uuid

    brand_id   = os.getenv("BRAND_ID", "bra_2")
    brand_name = os.getenv("BRAND_NAME", "Demo Brand")

    if len(sys.argv) >= 3:
        brand_id, brand_name = sys.argv[1], sys.argv[2]

    session_id = "cli_session"
    print(f"\n{'═'*60}\n  FashionOS — {brand_name} ({brand_id})\n{'═'*60}\n")

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