"""
FashionOS Deep Agent — Pipeline Trigger Tool
==============================================
Replaces deep_agents/subagents/*.py (inventory_agent, trend_agent, pricing_agent,
restock_agent, marketing_agent, content_agent, dm_agent — all deleted).

Why this exists:
  Before: deep_agents/subagents/{X}_agent.py duplicated the domain logic already
  built into agents/{X}/graph.py (LangGraph). Two independent implementations of
  "how pricing decisions get made" — one used by chat, one used by webhooks/beat —
  drift out of sync the moment either is tuned and the other is forgotten.
  (restock_agent.py was already dead code — built but never wired into
  deep_agents/supervisor.py's subagents=[...] list. Exactly this failure mode.)

  Now: the deep agent calls ONE tool. That tool runs the SAME LangGraph pipeline
  (agents/supervisor.py) used by Celery/webhooks — same prompts, same MCP calls,
  same DB writes — but ONLY the node(s) actually needed for this request.

Dependency expansion:
  Agents read state written by upstream agents (pricing reads inventory_snapshot +
  trend_signals; marketing reads all three; content reads all four). Requesting an
  agent without its prerequisites means it runs on empty state and reasons blind.
  AGENT_DEPENDS_ON expands every request to include prerequisites, in the correct
  order, before the pipeline runs. Every OTHER node in the graph still self-skips
  almost instantly (see agents/supervisor.py's run_*_agent functions — each checks
  `if "X" not in state.get("agents_to_run", []): return {}` first) — so requesting
  agents=["pricing"] does NOT run all 8 agents. It runs exactly 3: inventory, trend,
  pricing. Everything else no-ops with no LLM call and no MCP call.

Side-effect warning:
  inventory, trend, returns, content, restock  → no external writes, safe to run freely.
  pricing   → can auto-execute real Shopify price / compare_at_price changes.
  marketing → can auto-execute real Meta campaign pauses / budget decreases.
  dm        → can auto-send real Instagram DM replies to real customers.
  This tool does not gate on that itself — deep_agents/supervisor.py's system
  prompt is responsible for telling the founder what could auto-execute and
  getting explicit confirmation before calling this tool with "pricing",
  "marketing", or "dm" in the agent list. Same trust model as any other
  consequential action tool in this codebase.
"""

from typing import Literal

AgentName = Literal[
    "inventory", "trend", "pricing", "restock",
    "marketing", "content", "returns", "dm",
]

# Prerequisite agents that MUST run first so the requested agent reads real
# upstream state rather than empty lists. Mirrors the "Execution order is
# mandatory" section of each fashion-*/SKILL.md and decide_agents()'s daily
# sweep order in agents/supervisor.py.
AGENT_DEPENDS_ON: dict[str, list[str]] = {
    "inventory": [],
    "trend":     ["inventory"],
    "pricing":   ["inventory", "trend"],
    "restock":   ["inventory", "pricing"],
    "marketing": ["inventory", "trend", "pricing"],
    "content":   ["inventory", "trend", "pricing", "marketing"],
    "returns":   [],
    "dm":        [],
}

# Agents whose run can execute real-world side effects. Referenced only in
# docstrings/warnings here — the actual confirmation gate lives in the
# supervisor's system prompt, since only the LLM in the conversation knows
# whether the founder already agreed this turn.
SIDE_EFFECT_AGENTS = {"pricing", "marketing", "dm"}


def _expand(requested: list[str]) -> list[str]:
    """
    Expands a requested agent list to include all prerequisites, preserving
    dependency order and de-duplicating.
    e.g. ["pricing"]   -> ["inventory", "trend", "pricing"]
         ["marketing"] -> ["inventory", "trend", "pricing", "marketing"]
         ["dm"]        -> ["dm"]   (no dependencies)
    """
    ordered: list[str] = []

    def _add(name: str) -> None:
        for dep in AGENT_DEPENDS_ON.get(name, []):
            _add(dep)
        if name not in ordered:
            ordered.append(name)

    for a in requested:
        _add(a)
    return ordered


async def start_agent_analysis(
    brand_id: str,
    brand_name: str,
    agents: list[str],
) -> dict:
    """
    NON-BLOCKING. Queues the requested agent(s) — plus whatever they structurally
    depend on — on the FashionOS Celery pipeline (the exact same task used by
    Shopify webhooks and the scheduled sweeps) and returns IMMEDIATELY, typically
    within milliseconds. It does NOT wait for the pipeline to finish.

    THIS IS INTENTIONAL. After calling this, tell the founder the analysis has
    started and roughly how long it'll take (a single agent: ~10-20s, a chain
    of 3-4: ~30-60s) — do NOT sit silently waiting for results in the same turn,
    there aren't any yet. Then, either:
      - if the founder's next message asks whether it's ready, or
      - proactively, if you know a job is in flight for this brand,
    call get_pipeline_status(brand_id) to check. Compare its last_run_at /
    hours_ago against when you started this job:
      - if last_run_at is AFTER you called start_agent_analysis -> it landed.
        Report the fresh numbers from get_pipeline_status / get_inventory_status /
        get_pending_approvals / etc. as appropriate to what was asked.
      - if last_run_at is still the OLD timestamp -> still running. Tell the
        founder it's not done yet, offer to check again shortly.

    Every agent NOT in the expanded list self-skips inside the graph almost
    instantly (no LLM call, no MCP call) once the worker picks up the job —
    so requesting agents=["pricing"] does NOT run all 8 agents. It runs exactly:
    inventory -> trend -> pricing.

    Valid agent names: "inventory", "trend", "pricing", "restock", "marketing",
    "content", "returns", "dm".

    ⚠ SIDE EFFECTS — read before calling with "pricing", "marketing", or "dm":
      pricing   can auto-execute real Shopify price / compare_at_price changes
                (first-rung markdowns, trending price increases, clearance codes
                within auto-execute thresholds — see fashion-pricing SKILL.md).
      marketing can auto-execute real Meta Ads changes (pausing out-of-stock or
                clearance campaigns, decreasing budget on low ROAS or organic
                viral SKUs — see fashion-marketing SKILL.md).
      dm        can auto-send real Instagram DM replies to real customers
                (size/availability/order-status/general/pricing categories —
                see fashion-dm SKILL.md).
      You MUST have already told the founder what could auto-execute and gotten
      explicit confirmation before calling this tool with any of these three.
      inventory, trend, returns, content, and restock have NO external side
      effects (restock only ever writes pending_approval rows) — call freely
      whenever fresh data is genuinely needed, no confirmation required.

    Args:
        brand_id:   The brand to run analysis for.
        brand_name: The brand's display name.
        agents:     Agent name(s) actually needed, e.g. ["pricing"] or ["dm"].
                    Do NOT pass all 8 unless the founder explicitly asked for a
                    full sweep — prefer the narrowest list that answers the question.

    Returns:
        {
            "status": "queued",
            "task_id": "...",                                  # Celery task id
            "requested_agents": [...],                         # what you asked for
            "expanded_agents": ["inventory", "trend", "pricing"],  # what will actually run
            "note": "<reminder to acknowledge now, check status later>"
        }
        or {"error": "..."} if the job couldn't even be queued (e.g. Redis down).
    """
    from api.workers.tasks import run_agent_pipeline

    expanded = _expand(agents)

    try:
        task = run_agent_pipeline.delay(
            brand_id=brand_id,
            brand_name=brand_name,
            trigger="manual",
            trigger_payload={"source": "chat", "requested_agents": agents},
            agents_to_run=expanded,
        )
        return {
            "status": "queued",
            "task_id": task.id,
            "requested_agents": agents,
            "expanded_agents": expanded,
            "note": (
                "Pipeline is running in the background now. Tell the founder it's "
                "started and roughly how long to expect — do not wait silently for "
                "it here. Check get_pipeline_status(brand_id) on a later turn (or "
                "if asked) to fetch the completed results once it lands."
            ),
        }
    except Exception as exc:
        return {
            "error": f"Could not queue pipeline run: {exc}",
            "requested_agents": agents,
            "expanded_agents": expanded,
        }


def check_agent_analysis_status(task_id: str) -> dict:
    """
    Checks the exact status of a pipeline run previously queued via
    start_agent_analysis. Non-blocking — returns immediately with whatever
    state the Celery task is currently in. This is precise (asks Celery
    directly about THIS task_id) rather than inferring from run timestamps.

    Call this:
      - if the founder asks whether the analysis is ready / done / finished
      - proactively, on your next turn, if you know a job is in flight and
        haven't reported back on it yet

    Args:
        task_id: The task_id string returned by start_agent_analysis.

    Returns:
        {"status": "pending"}                    — queued, not yet picked up
        {"status": "running"}                     — a worker has started it
        {"status": "done", "result": {...}}       — finished; result is the
                                                     same summary dict shape
                                                     run_agent_pipeline returns
                                                     (completed_agents, run_summary,
                                                     alert_counts, pricing, marketing,
                                                     dm, ...) — use it directly to
                                                     tell the founder what happened,
                                                     no need for a separate DB call.
        {"status": "failed", "error": "..."}      — the pipeline raised; tell the
                                                     founder plainly, don't retry
                                                     silently.
    """
    from celery.result import AsyncResult
    from api.workers.tasks import celery_app

    result = AsyncResult(task_id, app=celery_app)

    if result.state == "PENDING":
        return {"status": "pending"}
    if result.state in ("STARTED", "RETRY"):
        return {"status": "running"}
    if result.state == "SUCCESS":
        return {"status": "done", "result": result.result}
    if result.state == "FAILURE":
        return {"status": "failed", "error": str(result.result)}
    return {"status": result.state.lower()}


def get_pipeline_tools() -> list:
    """Pass to create_deep_agent(tools=get_db_tools() + get_pipeline_tools())."""
    return [start_agent_analysis, check_agent_analysis_status]