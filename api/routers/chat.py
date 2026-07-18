"""
FashionOS — Chat API Router
============================
POST  /api/v1/chat                              — non-streaming
POST  /api/v1/chat/stream                       — SSE streaming
GET   /api/v1/conversations                     — list all conversations
GET   /api/v1/conversations/{thread_id}/messages — message history
DELETE /api/v1/conversations/{thread_id}        — delete conversation metadata

SSE event format:  data: <json>\n\n
  {"type":"token",          "content":"..."}
  {"type":"subagent_start", "name":"inventory-agent"}
  {"type":"subagent_token", "name":"inventory-agent", "content":"..."}
  {"type":"subagent_done",  "name":"inventory-agent", "summary":"..."}
  {"type":"done"}
  {"type":"error",          "content":"..."}

Conversation metadata is stored in Redis (AsyncRedisStore, namespace convos/{brand_id}).
Message history is replayed from the LangGraph AsyncRedisSaver checkpoint.
"""

import json
import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_brand
from db.models import Brand
from db.session import get_session

router = APIRouter(prefix="/api/v1", tags=["chat"])


# ── Models ─────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:   str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    brand_id: str


class ConversationMeta(BaseModel):
    thread_id:  str
    title:      str
    created_at: str
    updated_at: str


class ToolResultOut(BaseModel):
    name:    str
    summary: str
    data:    list | dict | None = None
    status:  str = "done"


class MessageOut(BaseModel):
    role:         str   # "user" | "assistant"
    content:      str
    tool_results: list[ToolResultOut] = []
    reasoning:    str = ""


# ── Non-streaming chat ─────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    req:   ChatRequest,
    brand: Brand = Depends(get_current_brand),
) -> ChatResponse:
    from deep_agents.supervisor import chat as supervisor_chat

    try:
        response = await supervisor_chat(
            brand_id   = brand.brand_id,
            brand_name = brand.brand_name,
            message    = req.message,
            thread_id  = req.thread_id,
        )
        return ChatResponse(response=response, brand_id=brand.brand_id)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


# ── Streaming chat ─────────────────────────────────────────────────────────────

@router.post("/chat/stream", response_class=StreamingResponse)
async def chat_stream_endpoint(
    req:   ChatRequest,
    brand: Brand = Depends(get_current_brand),
) -> StreamingResponse:
    from deep_agents.supervisor import stream_chat, save_conversation_meta

    # Upsert conversation metadata before streaming begins.
    # Title = first 72 chars of the message (trimmed).
    title = req.message.strip()[:72] + ("…" if len(req.message.strip()) > 72 else "")
    await save_conversation_meta(
        brand_id  = brand.brand_id,
        thread_id = req.thread_id,
        title     = title,
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in stream_chat(
                brand_id   = brand.brand_id,
                brand_name = brand.brand_name,
                message    = req.message,
                thread_id  = req.thread_id,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            return
        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','content':str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Conversation list ──────────────────────────────────────────────────────────

@router.get("/conversations", response_model=list[ConversationMeta])
async def list_conversations_endpoint(
    brand: Brand = Depends(get_current_brand),
) -> list[ConversationMeta]:
    from deep_agents.supervisor import list_conversations

    try:
        convos = await list_conversations(brand.brand_id)
        result = []
        for c in convos:
            try:
                result.append(ConversationMeta(**c))
            except Exception as item_exc:
                # Log and skip malformed items rather than blowing up the whole list
                print(f"[chat] skipping malformed convo item {c!r}: {item_exc}")
        return result
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


# ── Message history ────────────────────────────────────────────────────────────

@router.get(
    "/conversations/{thread_id}/messages",
    response_model=list[MessageOut],
)
async def get_conversation_messages(
    thread_id: str,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    from collections import defaultdict

    from sqlalchemy import select

    from db.models import ChatToolResult
    from deep_agents.supervisor import get_thread_messages
    from deep_agents.streaming import REASONING_SENTINEL

    try:
        # Checkpoint (Redis) and tool-results (Postgres) are independent —
        # fetch concurrently instead of round-tripping one after the other.
        msgs, tool_result_rows = await asyncio.gather(
            get_thread_messages(
                brand_id   = brand.brand_id,
                brand_name = brand.brand_name,
                thread_id  = thread_id,
            ),
            session.execute(
                select(ChatToolResult)
                .where(ChatToolResult.brand_id  == brand.brand_id)
                .where(ChatToolResult.thread_id == thread_id)
                .order_by(ChatToolResult.turn_index, ChatToolResult.created_at)
            ),
        )
        rows = tool_result_rows.scalars().all()

        # Group by turn_index (0-based index of assistant messages). The
        # reasoning sentinel row is pulled out separately — it's persisted
        # text for the ReasoningBlock, not a tool-result card.
        tool_results_by_turn: dict[int, list[ToolResultOut]] = defaultdict(list)
        reasoning_by_turn: dict[int, str] = {}
        for row in rows:
            if row.label == REASONING_SENTINEL:
                reasoning_by_turn[row.turn_index] = row.summary or ""
                continue
            tool_results_by_turn[row.turn_index].append(ToolResultOut(
                name    = row.label,
                summary = row.summary or "",
                data    = row.data,
                status  = "done",
            ))

        # Merge: match each assistant message to its subagent results by position
        enriched: list[MessageOut] = []
        asst_idx = 0
        for m in msgs:
            if m.get("role") == "assistant":
                enriched.append(MessageOut(
                    role      = m["role"],
                    content   = m.get("content", ""),
                    tool_results = tool_results_by_turn.get(asst_idx, []),
                    reasoning = reasoning_by_turn.get(asst_idx, ""),
                ))
                asst_idx += 1
            else:
                enriched.append(MessageOut(
                    role    = m.get("role", "user"),
                    content = m.get("content", ""),
                ))
        return enriched

    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


# ── Delete conversation ────────────────────────────────────────────────────────

@router.delete("/conversations/{thread_id}", status_code=204)
async def delete_conversation_endpoint(
    thread_id: str,
    brand:     Brand = Depends(get_current_brand),
) -> None:
    from deep_agents.supervisor import delete_conversation

    try:
        await delete_conversation(brand.brand_id, thread_id)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))