"""
FashionOS Deep Agent — Chat Entrypoints
==========================================
chat()         — non-streaming, returns the final assistant text.
stream_chat()  — streaming, yields SSE-ready event dicts for api/routers/chat.py.

Also owns tool-result persistence: which tool outputs get written to
chat_subagent_results (reused table — see db/models.py) so the frontend can
render a rich card when conversation history is reloaded, not just prose.
"""

import asyncio
import json
from collections.abc import AsyncGenerator

from pydantic import BaseModel as _PydanticBase
from requests import session

from deep_agents.runtime import get_cached_agent

# Tool results worth persisting for conversation-history replay. Keep in sync
# with deep_agents/tools/db_tools.py + deep_agents/tools/pipeline_tools.py.
# read_file/edit_file (AGENTS.md memory ops) are deliberately excluded — not
# "data" worth replaying as a card.
PERSISTABLE_TOOLS: set[str] = {
    "start_agent_analysis",
    "check_agent_analysis_status",
    "get_pipeline_status",
    "get_inventory_status",
    "get_critical_skus",
    "get_open_alerts",
    "get_pending_approvals",
    "get_sku_history",
    "get_return_insights",
    "get_content_queue",
    "get_run_history",
}

# Sentinel label for persisted reasoning rows in chat_tool_results.
# Reasoning isn't a tool result — it's the model's own thinking for the turn —
# but reusing the same table avoids a second one. get_conversation_messages()
# in api/routers/chat.py pulls rows with this label out separately from the
# real tool-result cards.
REASONING_SENTINEL = "__reasoning__"


async def _save_reasoning(
    brand_id: str,
    thread_id: str,
    turn_index: int,
    reasoning_text: str,
) -> None:
    """
    Fire-and-forget: persist the full reasoning text for this turn so the
    ReasoningBlock survives a page reload / switching conversations, instead
    of vanishing the moment the live SSE stream ends.
    """
    from db.session import AsyncSessionLocal
    from db.models  import ChatToolResult

    try:
        async with AsyncSessionLocal() as session:
            session.add(ChatToolResult(
                brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
                label=REASONING_SENTINEL, summary=reasoning_text, data=None,
            ))

            await session.commit()
    except Exception as exc:
        print(f"[Streaming] ⚠ Failed to persist reasoning: {exc}")


async def _save_tool_result(
    brand_id: str,
    thread_id: str,
    turn_index: int,
    tool_name: str,
    summary: str,
    data: dict,
    call_seq: int = 0,
) -> None:
    """
    Fire-and-forget: persist one tool result row so the frontend can render a
    rich card when conversation history is reloaded, not just prose.

    call_seq: 0-based counter of how many times this tool_name has already
    been persisted in this turn. When > 0, a "#N" suffix is appended to the
    label so duplicate tool calls (e.g. get_inventory_status called twice)
    each get a unique label and are ALL saved — previously the 2nd+ call
    would silently fail if the DB row already existed for that label.
    """
    from db.session import AsyncSessionLocal
    from db.models  import ChatToolResult

    label = tool_name
    if tool_name == "check_agent_analysis_status":
        completed = (data.get("result") or {}).get("completed_agents")
        if completed:
            label = ",".join(completed)

    # Append a sequence suffix so two calls to the same tool in one turn
    # produce different labels (e.g. "get_inventory_status" vs
    # "get_inventory_status#2").  Without this the second INSERT either
    # violates a unique constraint or overwrites the first row — either way
    # the second tool card disappears on page reload.
    if call_seq > 0:
        label = f"{label}#{call_seq + 1}"

    # Truncate to fit the label column (255 chars in DB).
    label = label[:255]

    if not isinstance(summary, str):
        if isinstance(summary, dict):
            summary = ", ".join(f"{k}: {v}" for k, v in summary.items())
        elif isinstance(summary, list):
            summary = ", ".join(str(item) for item in summary)
        else:
            summary = str(summary) if summary is not None else ""

    try:
        async with AsyncSessionLocal() as session:
            session.add(ChatToolResult(
                brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
                label=label, summary=summary, data=data,
            ))
            await session.commit()
    except Exception as exc:
        print(f"[Streaming] ⚠ Failed to persist tool result ({tool_name} seq={call_seq}): {exc}")


# ── Non-streaming chat ──────────────────────────────────────────────────────

async def chat(brand_id: str, brand_name: str, message: str, thread_id: str = "default") -> str:
    agent         = await get_cached_agent(brand_id, brand_name)
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


# ── Streaming chat ──────────────────────────────────────────────────────────

async def stream_chat(
    brand_id: str,
    brand_name: str,
    message: str,
    thread_id: str = "default",
) -> AsyncGenerator[dict, None]:
    """
    Yields SSE-ready event dicts:
      {"type": "token",       "content": "..."}
      {"type": "reasoning",   "name": None, "content": "..."}
      {"type": "tool_call",   "name": "...", "id": "...", "args": {...}}
      {"type": "tool_result", "name": "...", "id": "...", "data": ...}
      {"type": "done"}
      {"type": "error",       "content": "..."}
    """
    agent         = await get_cached_agent(brand_id, brand_name)
    scoped_thread = f"{brand_id}:{thread_id}"
    config        = {"configurable": {"thread_id": scoped_thread}}

    try:
        state         = await agent.aget_state(config)
        existing_msgs = (state.values or {}).get("messages", []) or []
        turn_index    = sum(1 for m in existing_msgs if getattr(m, "type", "") == "ai")
    except Exception:
        turn_index = 0

    # Maps tool_call_id → tool name, so we can label the matching ToolMessage
    # result when it streams back on a LATER chunk.
    tc_id_to_tool: dict[str, str] = {}
    reasoning_accum = ""   # full reasoning text for this turn — saved once the stream ends

    # Tracks how many times each tool name has been persisted this turn so
    # duplicate calls (e.g. get_inventory_status × 2) each get a unique label.
    tool_persist_count: dict[str, int] = {}

    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
        ):
            if not isinstance(chunk, tuple) or len(chunk) != 2:
                continue

            msg_chunk, _metadata = chunk

            # # Ollama/ChatOllama (qwen3.5 etc.) puts reasoning in additional_kwargs
            # # under "reasoning_content" — NOT as a {"type": "reasoning", ...}
            # # content block the way Kimi/Azure does further below. These two
            # # paths are mutually exclusive per-provider (a chunk from Kimi
            # # won't have reasoning_content in additional_kwargs, and a chunk
            # # from Ollama won't have reasoning blocks in content), so there's
            # # no double-counting risk running both.
            # reasoning_kw = (getattr(msg_chunk, "additional_kwargs", {}) or {}).get("reasoning_content", "")
            # if reasoning_kw:
            #     reasoning_accum += reasoning_kw
            #     yield {"type": "reasoning", "name": None, "content": reasoning_kw}

            raw = getattr(msg_chunk, "content", "") or ""
            if isinstance(raw, _PydanticBase):
                raw = raw.model_dump_json()

            # ── Split content blocks: reasoning vs. text ────────────────────
            # Some providers return a list of content blocks:
            #   {"type": "reasoning", "summary": [{"text": "..."}]}                  ← Kimi/Azure (model1)
            #   {"type": "thinking", "thinking": [{"type": "text", "text": "..."}]}  ← Mistral (model2, reasoning_effort="high")
            #   {"type": "text", "text": "..."}
            # Others just return a plain string — handled by the else branch.
            if isinstance(raw, list):
                reasoning_parts, text_parts = [], []
                for c in raw:
                    if isinstance(c, dict):
                        block_type = c.get("type")
                        if block_type == "reasoning":
                            for s in c.get("summary", []) or []:
                                reasoning_parts.append(s.get("text", "") if isinstance(s, dict) else str(s))
                            if c.get("text"):
                                reasoning_parts.append(c["text"])
                        elif block_type == "thinking":
                            # Mistral ThinkChunk — "thinking" is a list of TextChunk
                            # objects, not a plain string like Kimi's "summary".
                            for s in c.get("thinking", []) or []:
                                reasoning_parts.append(s.get("text", "") if isinstance(s, dict) else str(s))
                        elif block_type == "text":
                            text_parts.append(c.get("text", ""))
                        elif "text" in c:
                            text_parts.append(c.get("text", ""))
                    else:
                        text_parts.append(str(c))
                reasoning_text = "".join(reasoning_parts)
                text           = "".join(text_parts)
            else:
                reasoning_text = ""
                text           = str(raw)

            if reasoning_text:
                reasoning_accum += reasoning_text
                yield {"type": "reasoning", "name": None, "content": reasoning_text}

            # ── Detect tool-call requests on this chunk ─────────────────────
            for tc in (getattr(msg_chunk, "tool_calls", []) or []):
                tc_name = tc.get("name")
                tc_id   = tc.get("id", "")
                if tc_name and tc_id:
                    tc_id_to_tool[tc_id] = tc_name
                    yield {
                        "type": "tool_call",
                        "name": tc_name,
                        "id":   tc_id,
                        "args": tc.get("args") or {},
                    }

            # ── Intercept ToolMessage results ───────────────────────────────
            # BUG FIX: this must be a SIBLING check, not nested inside the
            # tool_calls loop above. A ToolMessage chunk carries tool_call_id,
            # not tool_calls — the previous version buried this inside
            # `for tc in tool_calls`, so it only ran (redundantly) when the
            # CURRENT chunk happened to also have tool_calls, and never fired
            # for the actual ToolMessage chunk carrying the result.
            tc_id = getattr(msg_chunk, "tool_call_id", None)
            if tc_id and tc_id in tc_id_to_tool:
                tool_name = tc_id_to_tool.pop(tc_id)
                parsed: object = text
                if text:
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        pass

                yield {"type": "tool_result", "name": tool_name, "id": tc_id, "data": parsed}

                is_valid_result = False
                if tool_name in PERSISTABLE_TOOLS:
                    if isinstance(parsed, dict) and "error" not in parsed:
                        is_valid_result = True
                    elif isinstance(parsed, list):
                        is_valid_result = True

                if is_valid_result:
                    if isinstance(parsed, dict):
                        summary_raw = (
                            parsed.get("run_summary")
                            or (parsed.get("result") or {}).get("run_summary")
                            or parsed.get("summary")
                            or ""
                        )
                    else:
                        summary_raw = f"{len(parsed)} items"

                    if isinstance(summary_raw, dict):
                        summary_text = ", ".join(f"{k}: {v}" for k, v in summary_raw.items())
                    elif isinstance(summary_raw, list):
                        summary_text = ", ".join(str(item) for item in summary_raw)
                    else:
                        summary_text = str(summary_raw)

                    # Determine the sequence number for this tool call within
                    # the current turn so duplicate calls get unique labels.
                    persist_key = tool_name
                    call_seq = tool_persist_count.get(persist_key, 0)
                    tool_persist_count[persist_key] = call_seq + 1

                    asyncio.ensure_future(_save_tool_result(
                        brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
                        tool_name=tool_name, summary=summary_text, data=parsed,
                        call_seq=call_seq,
                    ))

                continue  # never emit raw tool JSON as chat tokens

            # ── Everything else with text → regular assistant token ─────────
            # BUG FIX: previously gated behind `if node in SUBAGENT_NAMES`.
            # SUBAGENT_NAMES is now permanently empty (no more subagent graph
            # nodes), which meant this condition could never be true — ALL
            # assistant text was silently swallowed, never streamed to the
            # user. There is no more per-node routing to do; any remaining
            # text on a chunk is ordinary assistant output.
            if text:
                yield {"type": "token", "content": text}

    except Exception as exc:
        yield {"type": "error", "content": str(exc)}

    if reasoning_accum:
        asyncio.ensure_future(_save_reasoning(
            brand_id=brand_id, thread_id=thread_id, turn_index=turn_index,
            reasoning_text=reasoning_accum,
        ))

    yield {"type": "done"}