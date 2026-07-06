"""
FashionOS Deep Agent Supervisor — Entry Point
================================================
Thin orchestrator. All actual logic now lives in:
  deep_agents/serialization.py   — Redis/Pydantic checkpoint serialization patches
  deep_agents/memory.py          — AGENTS.md long-term memory seeding
  deep_agents/prompts.py         — system prompt
  deep_agents/runtime.py         — singletons (store, checkpointer, agent cache) + agent factory
  deep_agents/conversations.py   — conversation metadata CRUD + message replay
  deep_agents/streaming.py       — chat() / stream_chat() + tool-result persistence

This file exists so api/routers/chat.py's existing imports
(`from deep_agents.supervisor import ...`) keep working unchanged — nothing
in that router needed to change for this split.

Architecture summary:
  SHORT-TERM: automatic via LangGraph + thread_id (brand_id:session_id scoped).
  LONG-TERM:  /memories/AGENTS.md, StoreBackend namespaced per brand_id.
  EPHEMERAL:  StateBackend() for /workspace/ scratch, gone after conversation.
  FRESH DATA: start_agent_analysis / check_agent_analysis_status — queues the
              real LangGraph pipeline (agents/supervisor.py) in the background;
              the deep agent never talks to Shopify/Meta/Instagram directly.
"""

import asyncio

from deep_agents.runtime import get_cached_agent
from deep_agents.streaming import chat, stream_chat, PERSISTABLE_TOOLS
from deep_agents.conversations import (
    save_conversation_meta,
    list_conversations,
    delete_conversation,
    get_thread_messages,
)

__all__ = [
    "chat",
    "stream_chat",
    "save_conversation_meta",
    "list_conversations",
    "delete_conversation",
    "get_thread_messages",
    "get_cached_agent",
    "PERSISTABLE_TOOLS",
]


# ── CLI ────────────────────────────────────────────────────────────────────────

async def _cli():
    import os
    import sys
    import uuid

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