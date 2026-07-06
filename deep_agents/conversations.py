"""
FashionOS Deep Agent — Conversation Metadata & Message Replay
=================================================================
Conversation list/create/delete metadata (stored in Redis via AsyncRedisStore,
namespace ("convos", brand_id)) plus replaying a thread's human/AI messages
from the LangGraph checkpoint for the chat history view.
"""

from datetime import datetime, timezone

from deep_agents.runtime import get_store, get_checkpointer, get_cached_agent

_CONVOS_NS = "convos"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Conversation metadata CRUD ─────────────────────────────────────────────────

async def save_conversation_meta(brand_id: str, thread_id: str, title: str) -> None:
    """
    Upsert conversation metadata in Redis.
    Namespace: (_CONVOS_NS, brand_id)   Key: thread_id
    Preserves created_at on updates.
    """
    store     = await get_store()
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
    store     = await get_store()
    namespace = (_CONVOS_NS, brand_id)
    items     = await store.asearch(namespace, limit=200)

    required = {"thread_id", "title", "created_at", "updated_at"}
    convos = [
        item.value for item in items
        if item.value
        and not item.value.get("_deleted")
        and required.issubset(item.value.keys())
    ]
    return sorted(convos, key=lambda x: x.get("updated_at", ""), reverse=True)


async def delete_conversation(brand_id: str, thread_id: str) -> None:
    """
    Remove conversation metadata from Redis.

    Tries hard-delete via store.adelete first. Falls back to a soft-delete
    marker so list_conversations() filters it out — covers LangGraph versions
    where adelete may not be implemented.
    """
    store     = await get_store()
    namespace = (_CONVOS_NS, brand_id)

    deleted = False
    try:
        await store.adelete(namespace, thread_id)
        deleted = True
        print(f"[Convos] hard-deleted {thread_id} for brand={brand_id}")
    except Exception as exc:
        print(f"[Convos] adelete unavailable ({exc}), using soft-delete marker")

    if not deleted:
        try:
            await store.aput(namespace, thread_id, {"_deleted": True})
        except Exception as exc2:
            print(f"[Convos] soft-delete also failed: {exc2}")
            raise


# ── Message replay ──────────────────────────────────────────────────────────

async def _extract_messages(raw_messages: list) -> list[dict]:
    """Turn a list of LangChain message objects or raw dicts into [{role, content}] — human + ai only."""
    result: list[dict] = []
    for msg in raw_messages:
        if isinstance(msg, dict):
            msg_type = msg.get("type", "")
            content  = msg.get("content", "")
        else:
            msg_type = getattr(msg, "type", "") or ""
            if not msg_type:
                cls      = msg.__class__.__name__
                msg_type = "human" if "Human" in cls else ("ai" if "AI" in cls else "other")
            content = getattr(msg, "content", "") or ""

        if msg_type not in ("human", "ai"):
            continue

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


async def get_thread_messages(brand_id: str, brand_name: str, thread_id: str) -> list[dict]:
    """
    Replay the human + AI messages for a thread.

    Strategy (most reliable first):
      1. agent.aget_state()        — fully deserialized LangChain message objects
      2. checkpointer.aget_tuple() — raw checkpoint, channel_values.messages
    Both fall back gracefully and return [] on failure.
    """
    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    try:
        agent        = await get_cached_agent(brand_id, brand_name)
        state        = await agent.aget_state(config)
        raw_messages = (state.values or {}).get("messages", []) if state else []
        if raw_messages:
            result = await _extract_messages(raw_messages)
            print(f"[Convos] aget_state returned {len(result)} messages for {thread_id}")
            return result
        print(f"[Convos] aget_state returned 0 messages for {thread_id}, trying checkpointer")
    except Exception as exc:
        print(f"[Convos] aget_state failed ({exc}), falling back to checkpointer")

    try:
        checkpointer = await get_checkpointer()
        cp_tuple     = await checkpointer.aget_tuple(config)
        if not cp_tuple:
            print(f"[Convos] no checkpoint found for {thread_id}")
            return []

        checkpoint   = cp_tuple.checkpoint or {}
        raw_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
        result = await _extract_messages(raw_messages)
        print(f"[Convos] checkpoint fallback returned {len(result)} messages for {thread_id}")
        return result
    except Exception as exc:
        print(f"[Convos] checkpoint fallback failed: {exc}")
        return []