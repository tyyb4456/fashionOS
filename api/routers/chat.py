"""
FashionOS — Chat API Router
============================
Conversational interface to the FashionOS deep agent supervisor.
The supervisor has brand-scoped memory, DB tools, and live Shopify subagent.

POST /api/v1/chat
  Body : { "message": "...", "history": [{"role": "user", "content": "..."}] }
  Returns: { "response": "...", "brand_id": "..." }

History management:
  The endpoint is stateless — no server-side session. The frontend must pass
  the full conversation history on every call (standard LLM chat pattern).
  Long-term memory persists automatically in /memories/AGENTS.md (disk).

Requires Clerk JWT auth. Brand context (brand_id, brand_name) is injected
automatically from the authenticated brand — no spoofing possible.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.auth import get_current_brand
from db.models import Brand

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatMessage(BaseModel):
    role:    str   # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ChatResponse(BaseModel):
    response: str
    brand_id: str


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Chat with FashionOS Supervisor",
    description=(
        "Conversational interface to the FashionOS deep agent. "
        "The supervisor reads brand-specific memory, queries the DB for pipeline results, "
        "and can spawn live Shopify inventory subagents. "
        "Pass full conversation history on every call for multi-turn context."
    ),
)
async def chat_endpoint(
    req:   ChatRequest,
    brand: Brand = Depends(get_current_brand),
) -> ChatResponse:
    # Lazy import — avoids loading deepagents at API startup (heavy deps)
    from deep_agents.supervisor import chat as supervisor_chat

    try:
        history  = [{"role": m.role, "content": m.content} for m in req.history]
        response = await supervisor_chat(
            brand_id   = brand.brand_id,
            brand_name = brand.brand_name,
            message    = req.message,
            history    = history,
        )
        return ChatResponse(response=response, brand_id=brand.brand_id)

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Supervisor error: {exc}",
        )