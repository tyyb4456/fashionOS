"""
FashionOS Deep Agent — Runtime Singletons
============================================
Per-process singletons (Redis store, Redis checkpointer, per-brand agent
cache) and the agent factory itself. Split out from supervisor.py so both
supervisor.py (chat/stream_chat) and conversations.py (message replay) can
depend on this without importing each other — avoids circular imports.

Note: no MCP clients are built here. The deep agent never talks to
Shopify/Meta/Instagram directly — that only happens inside the real LangGraph
pipeline (agents/supervisor.py), reached exclusively via start_agent_analysis
(deep_agents/tools/pipeline_tools.py). The old SHOPIFY_MCP_URL / SOCIAL_MCP_URL
/ TRENDS_MCP_URL / ADS_MCP_URL constants and the HuggingFaceEndpoint/
ChatHuggingFace model experiment are gone — the latter was dead code anyway
(shadowed by `from deep_agents.load_model import llm` before it was ever used).
"""

import asyncio
import os
from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend, FilesystemBackend
from dotenv import load_dotenv
from langgraph.store.redis.aio import AsyncRedisStore
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langchain.agents.middleware import ModelFallbackMiddleware
from deep_agents.message_sanitizer import SanitizeMessagesMiddleware

from deep_agents.memory import ensure_brand_seeded
from deep_agents.prompts import build_prompt
from deep_agents.tools.db_tools import get_db_tools
from deep_agents.tools.pipeline_tools import get_pipeline_tools
from deep_agents.load_model import model1, model2
from deep_agents.turn_aware_fallback import TurnAwareModelFallback

load_dotenv()

REDIS_URL  = os.getenv("REDIS_URL", "redis://localhost:6379")
BASE_DIR   = Path(__file__).parent.resolve()
SKILLS_DIR = BASE_DIR / "skills"


# ── Singletons ────────────────────────────────────────────────────────────────

_store: AsyncRedisStore | None = None

async def get_store() -> AsyncRedisStore:
    global _store
    if _store is None:
        candidate = AsyncRedisStore(redis_url=REDIS_URL)
        await candidate.setup()     # if this throws, _store stays None — next request retries clean
        _store = candidate
        print("[Store] ✓ AsyncRedisStore ready (index created)")
    return _store


_checkpointer: AsyncRedisSaver | None = None

async def get_checkpointer() -> AsyncRedisSaver:
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = AsyncRedisSaver(redis_url=REDIS_URL)
        # No custom msgpack allowlist needed — every tool in the current
        # architecture (start_agent_analysis, check_agent_analysis_status,
        # get_db_tools()) returns plain dicts/lists, and LangChain message
        # types already have first-class serde support. The old allowlist
        # existed only for the now-deleted subagents' Pydantic response
        # schemas (InventoryAnalysis, TrendAnalysis, etc.) flowing directly
        # into message history.
        await _checkpointer.asetup()
        print("[Checkpointer] ✓ AsyncRedisSaver ready")
    return _checkpointer


# ── Per-brand agent cache ──────────────────────────────────────────────────────

_agent_cache: dict[str, object] = {}


async def build_supervisor(brand_id: str, brand_name: str):
    """
    Builds one deep agent instance for a brand: DB tools + pipeline tools
    (start_agent_analysis, check_agent_analysis_status), backed by Redis
    memory (/memories/AGENTS.md) and a read-only virtual /skills/ filesystem.
    """
    store, checkpointer = await asyncio.gather(get_store(), get_checkpointer())

    await ensure_brand_seeded(brand_id, brand_name, store)

    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(namespace=lambda rt: (brand_id,)),
            "/skills/":   FilesystemBackend(root_dir=str(SKILLS_DIR), virtual_mode=True),
        },
    )

    agent = create_deep_agent(
        name          = f"fashionos-{brand_id}",
        model         = model1,
        system_prompt = build_prompt(brand_id, brand_name),
        middleware    = [
            SanitizeMessagesMiddleware(),
            TurnAwareModelFallback(
                model2,
                mid_loop_retries=3,
                mid_loop_initial_delay=2.0,   # wait 2s, then 4s, then 8s
                mid_loop_backoff_factor=2.0,
            )
        ],
        tools         = get_db_tools() + get_pipeline_tools(),
        backend       = backend,
        store         = store,
        memory        = ["/memories/AGENTS.md"],
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


async def get_cached_agent(brand_id: str, brand_name: str):
    if brand_id not in _agent_cache:
        agent = await build_supervisor(brand_id, brand_name)
        _agent_cache[brand_id] = agent
        print(f"[Supervisor] ✓ Built + cached agent for brand={brand_id}")
    return _agent_cache[brand_id]