"""
deep_agents/turn_aware_fallback.py
====================================
Turn-aware model fallback — replaces bare ModelFallbackMiddleware(model2).

Problem with ModelFallbackMiddleware: it swaps models on ANY failed model
call, including calls in the middle of a tool-use loop. That hands the
fallback model (Ollama/qwen) message history it didn't produce and no shared
plan — it doesn't know what the primary model (Kimi) was mid-way through
doing, so it improvises: re-calling tools redundantly, or answering a
different implicit question instead of finishing the pending step. This is
exactly what happened with the "call me Tuddy" -> read_memory -> edit_file
flow: Kimi started the memory update, failed on the NEXT call, and qwen
picked up with no memory of what Kimi was doing.

Fix: only allow a model swap on the FIRST model call of a turn — i.e.
before any tool call has happened yet this turn (nothing sits between the
latest HumanMessage and the current call except plain AI/tool traffic with
no tool_calls). If a failure happens mid-loop (model1 already made at least
one tool call this turn), we do NOT swap models. We retry model1 itself a
bounded number of times instead, because several tools have real side
effects (Shopify price updates, Meta ad changes, DM sends — see prompts.py's
mandatory-confirmation rules for pricing/marketing/dm) and letting a
different model improvise into a half-finished loop risks duplicate or
incoherent actions. If model1 still can't recover, we raise loudly instead
of silently drifting to a different model's judgement.
"""

import asyncio
import time
from typing import Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, AIMessage


class MidTurnFallbackBlocked(Exception):
    """
    Raised when the primary model fails mid-tool-loop and retries on the
    SAME model are exhausted. We refuse to hand off to the fallback model
    at this point — surfaces as a clean error to the user/turn instead of a
    silent, incoherent model swap mid-task.
    """


def _is_first_model_call_of_turn(messages: list) -> bool:
    """
    Walk backwards from the end of the message list. If we reach a
    HumanMessage before we hit any AIMessage carrying tool_calls, no tool
    loop has started yet this turn — a model swap is safe here.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            return False  # a tool-call step already happened this turn
        if isinstance(msg, HumanMessage):
            return True   # reached the start of the turn cleanly, no tool calls yet
    return True  # empty / edge case — treat as turn start


# Tool calls that can have real, non-idempotent side effects once they've
# run — see prompts.py's mandatory-confirmation rules. "content" is included
# because prompts.py has it auto-queue "marketing" as a dependency.
_SIDE_EFFECT_AGENTS = {"pricing", "marketing", "dm", "content"}


def _messages_since_last_human(messages: list) -> list:
    """Slice of messages belonging to the current turn only."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i:]
    return messages


def _turn_has_side_effect_tool_call(turn_messages: list) -> bool:
    """
    True if a mutating tool call (start_agent_analysis with pricing/
    marketing/dm/content in its agents list) has already EXECUTED this
    turn. If so, we must not hand off to a different model — it has no
    reliable way to know the action already happened and could repeat it.
    """
    for msg in turn_messages:
        if isinstance(msg, AIMessage):
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc.get("name") == "start_agent_analysis":
                    agents = (tc.get("args") or {}).get("agents") or []
                    if _SIDE_EFFECT_AGENTS.intersection(agents):
                        return True
    return False


class TurnAwareModelFallback(AgentMiddleware):
    """
    - First model call of a turn fails  -> fall back to `fallback_model`, one retry.
    - Mid-loop model call fails         -> retry the SAME primary model up to
                                            `mid_loop_retries` times. No swap.
                                            If still failing, raise loudly.
    """

    def __init__(
        self,
        fallback_model,
        mid_loop_retries: int = 3,
        mid_loop_initial_delay: float = 2.0,
        mid_loop_backoff_factor: float = 2.0,
        mid_loop_max_delay: float = 20.0,
    ):
        super().__init__()
        self.fallback_model          = fallback_model
        self.mid_loop_retries        = mid_loop_retries
        self.mid_loop_initial_delay  = mid_loop_initial_delay
        self.mid_loop_backoff_factor = mid_loop_backoff_factor
        self.mid_loop_max_delay      = mid_loop_max_delay

    def _delay_for_attempt(self, attempt: int) -> float:
        # attempt 0 -> initial_delay, attempt 1 -> initial_delay * factor, ...
        delay = self.mid_loop_initial_delay * (self.mid_loop_backoff_factor ** attempt)
        return min(delay, self.mid_loop_max_delay)

    # ── sync ─────────────────────────────────────────────────────────────
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        turn_start = _is_first_model_call_of_turn(request.messages)

        try:
            return handler(request)
        except Exception as primary_exc:
            if turn_start:
                print(f"[Fallback] primary model failed at turn start ({primary_exc}); switching model")
                return handler(request.override(model=self.fallback_model))

            print(f"[Fallback] primary model failed MID-LOOP ({primary_exc}); retrying same model with backoff, no swap")
            last_exc = primary_exc
            for attempt in range(self.mid_loop_retries):
                delay = self._delay_for_attempt(attempt)
                print(f"[Fallback] waiting {delay:.1f}s before mid-loop retry {attempt + 1}/{self.mid_loop_retries}")
                time.sleep(delay)
                try:
                    return handler(request)
                except Exception as retry_exc:
                    last_exc = retry_exc
                    print(f"[Fallback] mid-loop retry {attempt + 1}/{self.mid_loop_retries} failed: {retry_exc}")

            turn_messages = _messages_since_last_human(request.messages)
            if _turn_has_side_effect_tool_call(turn_messages):
                raise MidTurnFallbackBlocked(
                    "Primary model failed mid-tool-loop after a side-effect tool call "
                    "(pricing/marketing/dm/content) already ran this turn, and retries "
                    "were exhausted. Refusing to hand off to the fallback model — it "
                    "can't safely know that action already happened. "
                    f"Original error: {last_exc}"
                ) from last_exc

            print("[Fallback] retries exhausted, no side-effect tool ran yet — "
                  "falling back to fallback model WITH full context preserved")
            return handler(request.override(model=self.fallback_model))

    # ── async (deep_agents runs async almost exclusively — this is the one that matters) ──
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler,
    ):
        turn_start = _is_first_model_call_of_turn(request.messages)

        try:
            return await handler(request)
        except Exception as primary_exc:
            if turn_start:
                print(f"[Fallback] primary model failed at turn start ({primary_exc}); switching model")
                return await handler(request.override(model=self.fallback_model))

            print(f"[Fallback] primary model failed MID-LOOP ({primary_exc}); retrying same model with backoff, no swap")
            last_exc = primary_exc
            for attempt in range(self.mid_loop_retries):
                delay = self._delay_for_attempt(attempt)
                print(f"[Fallback] waiting {delay:.1f}s before mid-loop retry {attempt + 1}/{self.mid_loop_retries}")
                await asyncio.sleep(delay)
                try:
                    return await handler(request)
                except Exception as retry_exc:
                    last_exc = retry_exc
                    print(f"[Fallback] mid-loop retry {attempt + 1}/{self.mid_loop_retries} failed: {retry_exc}")

            turn_messages = _messages_since_last_human(request.messages)
            if _turn_has_side_effect_tool_call(turn_messages):
                raise MidTurnFallbackBlocked(
                    "Primary model failed mid-tool-loop after a side-effect tool call "
                    "(pricing/marketing/dm/content) already ran this turn, and retries "
                    "were exhausted. Refusing to hand off to the fallback model — it "
                    "can't safely know that action already happened. "
                    f"Original error: {last_exc}"
                ) from last_exc

            print("[Fallback] retries exhausted, no side-effect tool ran yet — "
                  "falling back to fallback model WITH full context preserved")
            return await handler(request.override(model=self.fallback_model))