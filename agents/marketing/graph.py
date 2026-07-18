"""
Marketing Agent — FashionOS Phase 2 Operations (deterministic-math rewrite)
==============================================================================
Reads live Meta ad campaign data and cross-references it with trend_signals,
inventory_snapshot, and pricing_recommendations already in state. The
decision framework (SKU match → budget control → stock → clearance → trend
→ organic viral → ROAS → healthy) is a rule table, not judgment — so it's
now computed entirely in Python (Node 2). The LLM (Node 3) only writes
per-campaign reasons and a summary on top of numbers that are already final.

Graph topology (4 nodes, sequential):

    START
      │
      ▼
  fetch_campaign_data      ← Node 1: ads-mcp → get_campaigns() + per-campaign
      │                               get_campaign_performance(7d). Extracts
      │                               SKU from campaign name via regex.
      ▼
  compute_marketing_plan   ← Node 2: PURE PYTHON. Full decision framework,
      │                               budget math (round-to-50, ±30%/-50%
      │                               caps, PKR 200 floor → falls back to
      │                               pause below floor). No LLM.
      ▼
  generate_marketing_copy  ← Node 3: THE ONLY LLM CALL. Given the fully
      │                               computed plan, writes per-campaign
      │                               reasons for non-hold decisions + a
      │                               summary. Loads fashion_marketing
      │                               skill inline.
      ▼
  execute_marketing_actions← Node 4: Auto-execute pause + decrease_budget.
      │                               Queue increase_budget/activate as
      │                               pending_approval. Writes
      │                               marketing_actions + alerts.
      ▼
    END

Decision framework (Node 2, in priority order — matches fashion_marketing skill exactly):
  1. No SKU match (name doesn't follow FashionOS_{SKU}_{desc}) → hold, auto.
  2. No daily budget control (ad-set level budgets) → hold, auto.
  3. Out of stock (stock < 5) → pause, auto.
  4. Clearance SKU → pause, auto.
  5. Trending (score ≥ 0.5): ROAS ≥ 2.5 or no spend data → increase +25%,
     pending. ROAS < 2.5 with spend → hold (organic outperforms ads).
  6. Organic viral (velocity > 2x store average of selling SKUs) →
     decrease -30%, auto.
  7. Low ROAS (spend > PKR 500): ROAS < 0.8 → pause, auto.
     0.8 ≤ ROAS < 1.5 → decrease -20%, auto.
  8. Healthy → hold, auto.

Budget math: every target is rounded to the nearest PKR 50. If a decrease
would land below the PKR 200 floor, the action falls back to "pause"
instead (skill's explicit fallback rule) rather than silently under-shooting.

Chaining:
  Reads inventory_snapshot (Inventory), trend_signals (Trend),
  pricing_recommendations (Pricing) — all already in state.

Standalone test:
  python -m agents.marketing.graph
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Optional
import operator

from langchain_sambanova import ChatSambaNova
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import (
    AgentAlert,
    InventorySnapshot,
    MarketingAction,
    PricingRecommendation,
    TrendSignal,
)
from response_schemas.marketing_model import CampaignPlanItem, MarketingCopyPlan

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

ADS_MCP_URL = os.getenv("ADS_MCP_URL", "http://localhost:8004/mcp")



model = ChatSambaNova(
    model="Meta-Llama-3.3-70B-Instruct",
    # max_tokens=1024,
    temperature=0.7,
    top_p=0.01,
    # other params...
)

TRENDING_INCREASE_PCT   = float(os.getenv("MARKETING_TRENDING_INCREASE_PCT",  "25.0"))
ORGANIC_VIRAL_DECREASE  = float(os.getenv("MARKETING_ORGANIC_VIRAL_DECREASE", "30.0"))
LOW_ROAS_DECREASE_PCT   = float(os.getenv("MARKETING_LOW_ROAS_DECREASE_PCT",  "20.0"))
LOW_ROAS_SPEND_FLOOR    = float(os.getenv("MARKETING_LOW_ROAS_SPEND_FLOOR",   "500.0"))
LOW_ROAS_PAUSE_CEILING  = float(os.getenv("MARKETING_LOW_ROAS_PAUSE_CEILING", "0.8"))
LOW_ROAS_HOLD_CEILING   = float(os.getenv("MARKETING_LOW_ROAS_HOLD_CEILING",  "1.5"))
TRENDING_ROAS_FLOOR     = float(os.getenv("MARKETING_TRENDING_ROAS_FLOOR",    "2.5"))
TREND_SCORE_FLOOR       = float(os.getenv("MARKETING_TREND_SCORE_FLOOR",      "0.5"))
ORGANIC_VELOCITY_MULT   = float(os.getenv("MARKETING_ORGANIC_VELOCITY_MULT",  "2.0"))
OUT_OF_STOCK_THRESHOLD  = int(os.getenv("MARKETING_OUT_OF_STOCK_THRESHOLD",   "5"))
MIN_BUDGET_PKR          = float(os.getenv("MARKETING_MIN_BUDGET_PKR",         "200.0"))

_SKU_CONVENTION_RE = re.compile(r"FashionOS[_\-]([A-Z0-9\-]+)[_\-]", re.IGNORECASE)
_SKU_FALLBACK_RE   = re.compile(r"\b([A-Z]{2,5}-\d{3}(?:-[A-Z]{1,2})?)\b")

_HOLD_REASON_BY_TRIGGER = {
    "no_sku_match":          "Campaign name doesn't follow the FashionOS_{SKU}_{desc} convention — can't map to inventory. Hold until renamed.",
    "no_budget_control":     "Ad-set level budgets — can't be adjusted at campaign level via the API.",
    "trending_hold_low_roas":"Trending, but 7-day ROAS is below the 2.5x threshold — holding rather than spending into an inefficient campaign.",
    "healthy":               "Performance is healthy — no budget change needed.",
}


# ── Subgraph state ─────────────────────────────────────────────────────────────

class MarketingAgentState(TypedDict):
    brand_id:   str
    brand_name: str

    inventory_snapshot:      list[InventorySnapshot]
    trend_signals:           list[TrendSignal]
    pricing_recommendations: list[PricingRecommendation]

    # Node 1 output
    campaigns: list[dict]

    # Node 2 output (deterministic plan — internal scratch)
    computed_plan: list[dict]

    # LLM scratch
    raw_copy: str

    # Final outputs → operator.add merges safely with other agents
    marketing_actions: Annotated[list[MarketingAction], operator.add]
    alerts:            Annotated[list[AgentAlert],      operator.add]


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
    if (
        isinstance(raw, list)
        and len(raw) > 0
        and isinstance(raw[0], dict)
        and "text" in raw[0]
    ):
        return json.loads(raw[0]["text"])
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    content = getattr(raw, "content", str(raw))
    if isinstance(content, str):
        return json.loads(content)
    return content


def _extract_sku(campaign_name: str) -> Optional[str]:
    m = _SKU_CONVENTION_RE.search(campaign_name)
    if m:
        return m.group(1).upper()
    m = _SKU_FALLBACK_RE.search(campaign_name)
    if m:
        return m.group(1).upper()
    return None


def _round_budget(value: float) -> float:
    """Round to the nearest PKR 50."""
    return float(round(value / 50) * 50)


def _apply_budget_delta(current: float, delta_pct: float) -> tuple[Optional[float], float]:
    """
    Applies a % change to the current budget, rounds to nearest 50.
    Returns (new_budget, actual_change_pct). new_budget is None if the
    result would land below the PKR 200 floor — caller falls back to pause.
    """
    if current <= 0:
        return None, 0.0
    target  = current * (1 + delta_pct / 100)
    rounded = _round_budget(target)
    if rounded < MIN_BUDGET_PKR:
        return None, 0.0
    actual_pct = round((rounded - current) / current * 100, 1)
    return rounded, actual_pct


def _finalize(item: dict) -> dict:
    return CampaignPlanItem(**item).model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_campaign_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_campaign_data(state: MarketingAgentState) -> dict:
    """
    Fetches all active/paused campaigns from ads-mcp, then fetches 7-day
    performance for each ACTIVE campaign with a daily budget. Extracts SKU
    from campaign name via naming convention regex.
    """
    client   = MultiServerMCPClient(
        {"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    if "get_campaigns" not in tool_map:
        print("[Marketing] WARNING: get_campaigns not in tool_map — rebuild ads-mcp image")
        return {"campaigns": []}

    try:
        raw    = await tool_map["get_campaigns"].ainvoke({"active_only": True, "brand_id": state["brand_id"]})
        result = _parse_mcp_result(raw)
        if isinstance(result, list) and result and "error" in result[0]:
            print(f"[Marketing] ads-mcp error: {result[0]['error']}")
            return {"campaigns": []}
        campaigns_raw = result if isinstance(result, list) else []
    except Exception as exc:
        print(f"[Marketing] get_campaigns failed: {exc}")
        return {"campaigns": []}

    if not campaigns_raw:
        print("[Marketing] No active campaigns found.")
        return {"campaigns": []}

    campaigns_enriched: list[dict] = []
    for camp in campaigns_raw:
        campaign_id = camp["campaign_id"]
        sku         = _extract_sku(camp["name"])

        perf = {}
        if camp.get("has_daily_budget") and camp.get("status") == "ACTIVE" and "get_campaign_performance" in tool_map:
            try:
                raw_perf = await tool_map["get_campaign_performance"].ainvoke(
                    {"campaign_id": campaign_id, "days": 7, "brand_id": state["brand_id"]}
                )
                perf = _parse_mcp_result(raw_perf)
                if "error" in perf:
                    perf = {"no_spend_data": True}
            except Exception as exc:
                print(f"[Marketing] Performance fetch failed for {campaign_id}: {exc}")
                perf = {"no_spend_data": True}

        campaigns_enriched.append({
            "campaign_id":        campaign_id,
            "name":               camp["name"],
            "status":             camp.get("status", "UNKNOWN"),
            "daily_budget_pkr":   camp.get("daily_budget_pkr", 0.0),
            "has_daily_budget":   camp.get("has_daily_budget", False),
            "matched_sku":        sku,
            "follows_convention": sku is not None,
            "roas_7d":            perf.get("purchase_roas"),
            "spend_7d_pkr":       perf.get("spend_pkr", 0.0),
            "ctr_7d":             perf.get("ctr", 0.0),
            "no_spend_data":      perf.get("no_data", perf.get("no_spend_data", True)),
        })

    n_active   = sum(1 for c in campaigns_enriched if c["status"] == "ACTIVE")
    n_with_sku = sum(1 for c in campaigns_enriched if c["matched_sku"])
    print(f"[Marketing] {len(campaigns_enriched)} campaigns: {n_active} active, {n_with_sku} SKU-matched.")

    return {"campaigns": campaigns_enriched}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — compute_marketing_plan (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_marketing_plan(state: MarketingAgentState) -> dict:
    """
    The full decision framework — SKU match, budget control, stock, clearance,
    trend, organic viral, ROAS — is a rule table, computed in pure Python.
    The LLM never touches these decisions; it only writes prose on top in Node 3.
    """
    campaigns = state.get("campaigns", [])
    if not campaigns:
        print("[Marketing] No campaigns to analyse.")
        return {"computed_plan": []}

    inv_by_sku:     dict = {s["sku"]: s for s in state.get("inventory_snapshot", [])      if s.get("sku")}
    pricing_by_sku: dict = {p["sku"]: p for p in state.get("pricing_recommendations", []) if p.get("sku")}
    trending_by_sku: dict = {
        sig["matched_sku"]: sig for sig in state.get("trend_signals", [])
        if sig.get("matched_sku") and sig.get("direction") in ("rising", "peaking")
        and sig.get("score", 0) >= TREND_SCORE_FLOOR
    }

    # Store average velocity over SELLING SKUs only — including dead stock
    # would drag the average down and make "2x average" trivial to trigger.
    selling_velocities = [s.get("units_per_day", 0.0) for s in state.get("inventory_snapshot", []) if s.get("units_per_day", 0.0) > 0]
    store_avg_velocity = sum(selling_velocities) / len(selling_velocities) if selling_velocities else 0.0

    plan: list[dict] = []

    for camp in campaigns:
        sku   = camp.get("matched_sku")
        inv   = inv_by_sku.get(sku, {}) if sku else {}
        pri   = pricing_by_sku.get(sku, {}) if sku else {}
        trend = trending_by_sku.get(sku) if sku else None

        current_budget = camp["daily_budget_pkr"]
        roas           = camp.get("roas_7d")
        spend           = camp.get("spend_7d_pkr", 0.0)
        no_spend        = camp.get("no_spend_data", True)

        base = dict(
            campaign_id=camp["campaign_id"], campaign_name=camp["name"], sku=sku,
            follows_convention=camp["follows_convention"],
            current_status=camp["status"], has_daily_budget=camp["has_daily_budget"],
            current_budget_pkr=current_budget, roas_7d=roas, spend_7d_pkr=spend,
            ctr_7d=camp.get("ctr_7d", 0.0), no_spend_data=no_spend,
            action="hold", new_budget_pkr=None, change_pct=0.0,
            auto_execute=True, trigger="healthy",
        )

        # ── 1. No SKU match ──────────────────────────────────────────────────
        if not camp["follows_convention"] or not sku:
            base.update(trigger="no_sku_match")
            plan.append(_finalize(base))
            continue

        # ── 2. No budget control ─────────────────────────────────────────────
        if not camp["has_daily_budget"]:
            base.update(trigger="no_budget_control")
            plan.append(_finalize(base))
            continue

        current_stock  = inv.get("current_stock", 999)
        is_out_of_stock = current_stock < OUT_OF_STOCK_THRESHOLD
        is_clearance    = pri.get("action") == "clearance_code"

        # ── 3. Out of stock ───────────────────────────────────────────────────
        if is_out_of_stock:
            base.update(action="pause", auto_execute=True, trigger="out_of_stock")
            plan.append(_finalize(base))
            continue

        # ── 4. Clearance ─────────────────────────────────────────────────────
        if is_clearance:
            base.update(action="pause", auto_execute=True, trigger="clearance")
            plan.append(_finalize(base))
            continue

        # ── 5. Trending ───────────────────────────────────────────────────────
        if trend is not None:
            strong_roas = roas is not None and roas >= TRENDING_ROAS_FLOOR
            if no_spend or strong_roas:
                new_budget, change_pct = _apply_budget_delta(current_budget, TRENDING_INCREASE_PCT)
                base.update(
                    action="increase_budget", new_budget_pkr=new_budget, change_pct=change_pct,
                    auto_execute=False, trigger="trending_increase",
                )
            else:
                base.update(action="hold", auto_execute=True, trigger="trending_hold_low_roas")
            plan.append(_finalize(base))
            continue

        # ── 6. Organic viral ──────────────────────────────────────────────────
        velocity   = inv.get("units_per_day", 0.0)
        is_viral   = store_avg_velocity > 0 and velocity > (store_avg_velocity * ORGANIC_VELOCITY_MULT)
        if is_viral:
            new_budget, change_pct = _apply_budget_delta(current_budget, -ORGANIC_VIRAL_DECREASE)
            if new_budget is None:
                base.update(action="pause", auto_execute=True, trigger="organic_viral")
            else:
                base.update(
                    action="decrease_budget", new_budget_pkr=new_budget, change_pct=change_pct,
                    auto_execute=True, trigger="organic_viral",
                )
            plan.append(_finalize(base))
            continue

        # ── 7. Low ROAS ───────────────────────────────────────────────────────
        if roas is not None and spend > LOW_ROAS_SPEND_FLOOR:
            if roas < LOW_ROAS_PAUSE_CEILING:
                base.update(action="pause", auto_execute=True, trigger="low_roas_pause")
                plan.append(_finalize(base))
                continue
            if roas < LOW_ROAS_HOLD_CEILING:
                new_budget, change_pct = _apply_budget_delta(current_budget, -LOW_ROAS_DECREASE_PCT)
                if new_budget is None:
                    base.update(action="pause", auto_execute=True, trigger="low_roas_pause")
                else:
                    base.update(
                        action="decrease_budget", new_budget_pkr=new_budget, change_pct=change_pct,
                        auto_execute=True, trigger="low_roas_decrease",
                    )
                plan.append(_finalize(base))
                continue

        # ── 8. Healthy ────────────────────────────────────────────────────────
        base.update(action="hold", auto_execute=True, trigger="healthy")
        plan.append(_finalize(base))

    n_pause    = sum(1 for p in plan if p["action"] == "pause")
    n_increase = sum(1 for p in plan if p["action"] == "increase_budget")
    n_decrease = sum(1 for p in plan if p["action"] == "decrease_budget")
    n_hold     = sum(1 for p in plan if p["action"] == "hold")

    print(
        f"[Marketing] Plan computed: {len(plan)} campaigns — "
        f"{n_pause} pause, {n_increase} increase, {n_decrease} decrease, {n_hold} hold."
    )

    return {"computed_plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — generate_marketing_copy (the ONLY LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_marketing_copy(state: MarketingAgentState) -> dict:
    """
    Every number is already final. Writes a reason per non-hold campaign
    (holds get a deterministic reason from _HOLD_REASON_BY_TRIGGER in Node 4)
    and an overall summary.
    """
    plan       = state.get("computed_plan", [])
    actionable = [p for p in plan if p["action"] != "hold"]

    if not actionable:
        empty = MarketingCopyPlan(
            items=[],
            summary="No campaigns found, or all campaigns held this cycle — performance is healthy across the board.",
        )
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_marketing")

    compact = [
        {
            "campaign_id": p["campaign_id"], "campaign_name": p["campaign_name"], "sku": p["sku"],
            "action": p["action"], "trigger": p["trigger"],
            "current_budget_pkr": p["current_budget_pkr"], "new_budget_pkr": p["new_budget_pkr"],
            "change_pct": p["change_pct"], "roas_7d": p["roas_7d"], "spend_7d_pkr": p["spend_7d_pkr"],
        }
        for p in actionable
    ]

    system_prompt = f"""You are the Marketing Agent for {state['brand_name']}, an autonomous AI fashion brand operating system.

{skill_content}

## Your task
Every number below — action, new_budget_pkr, change_pct, trigger — is FINAL, computed by \
deterministic Python logic. Do NOT recompute, second-guess, or contradict any number. Write ONLY:

1. Per campaign: a 1-2 sentence `reason` referencing the given action, budget change, and trigger. \
   Example: "FOS-001-S is out of stock (3 units) — pausing to stop driving traffic to an unavailable product."
2. A 2-3 sentence overall `summary` — lead with what's paused/auto-executed, mention pending \
   budget increases with the most promising SKU.

## Output requirement
Include ALL campaigns listed below — one entry per campaign_id. Never omit one.
"""

    user_msg = (
        f"Marketing decisions for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Write the reasons and summary for the decisions above."
    )

    structured_llm = model.with_structured_output(MarketingCopyPlan)
    copy_plan: MarketingCopyPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[Marketing] Copy generated for {len(copy_plan.items)} campaigns. Summary: {copy_plan.summary}")

    return {"raw_copy": copy_plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_marketing_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_marketing_actions(state: MarketingAgentState) -> dict:
    """
    Auto-executes pause + decrease_budget via ads-mcp. Queues increase_budget
    and activate as pending_approval. Every campaign — including holds — is
    written to marketing_actions for full-picture dashboard display.
    """
    plan      = state.get("computed_plan", [])
    copy_plan = MarketingCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    reason_by_id = {c.campaign_id: c.reason for c in copy_plan.items}

    client   = MultiServerMCPClient(
        {"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    marketing_actions: list[MarketingAction] = []
    alerts:            list[AgentAlert]      = []

    for p in plan:
        reason = reason_by_id.get(p["campaign_id"]) or _HOLD_REASON_BY_TRIGGER.get(
            p["trigger"], "No action needed this cycle."
        )

        action_rec = MarketingAction(
            sku=p["sku"] or "", campaign_id=p["campaign_id"], campaign_name=p["campaign_name"],
            action=p["action"], reason=reason, auto_executed=p["auto_execute"] and p["action"] != "hold",
            trigger=p["trigger"],
            current_budget_pkr=p["current_budget_pkr"], new_budget_pkr=p["new_budget_pkr"],
            change_pct=p["change_pct"], roas_7d=p["roas_7d"], spend_7d_pkr=p["spend_7d_pkr"],
            ctr_7d=p["ctr_7d"],
        )

        # ── Auto-execute: pause ────────────────────────────────────────────────
        if p["auto_execute"] and p["action"] == "pause":
            if "pause_campaign" in tool_map:
                try:
                    await tool_map["pause_campaign"].ainvoke({
                        "campaign_id": p["campaign_id"], "reason": f"[AUTO] {reason}",
                        "brand_id": state["brand_id"],
                    })
                    print(f"[Marketing] ✓ Paused {p['campaign_id']} ({p['campaign_name']}): {p['trigger']}")
                    alerts.append(AgentAlert(
                        level="warning", agent="marketing_agent",
                        message=f"AUTO-PAUSED campaign '{p['campaign_name']}' (trigger: {p['trigger'].replace('_', ' ')}). Reason: {reason}",
                        sku=p["sku"], created_at=now_iso,
                    ))
                except Exception as exc:
                    print(f"[Marketing] ✗ Pause failed {p['campaign_id']}: {exc}")
                    alerts.append(AgentAlert(
                        level="warning", agent="marketing_agent",
                        message=f"Pause FAILED for campaign '{p['campaign_name']}': {exc}",
                        sku=p["sku"], created_at=now_iso,
                    ))
            else:
                print("[Marketing] WARNING: pause_campaign not in tool_map — rebuild ads-mcp image")

        # ── Auto-execute: decrease_budget ──────────────────────────────────────
        elif p["auto_execute"] and p["action"] == "decrease_budget" and p["new_budget_pkr"]:
            if "update_campaign_budget" in tool_map:
                try:
                    await tool_map["update_campaign_budget"].ainvoke({
                        "campaign_id": p["campaign_id"], "new_daily_budget_pkr": p["new_budget_pkr"],
                        "reason": f"[AUTO] {reason}", "brand_id": state["brand_id"],
                    })
                    print(
                        f"[Marketing] ✓ Budget decreased {p['campaign_id']}: "
                        f"PKR {p['current_budget_pkr']:.0f} → PKR {p['new_budget_pkr']:.0f} ({p['change_pct']:.0f}%)"
                    )
                    alerts.append(AgentAlert(
                        level="info", agent="marketing_agent",
                        message=(
                            f"AUTO: Budget decreased {abs(p['change_pct']):.0f}% on '{p['campaign_name']}' "
                            f"(PKR {p['current_budget_pkr']:.0f} → PKR {p['new_budget_pkr']:.0f}). Reason: {reason}"
                        ),
                        sku=p["sku"], created_at=now_iso,
                    ))
                except Exception as exc:
                    print(f"[Marketing] ✗ Budget update failed {p['campaign_id']}: {exc}")

        # ── Pending approval: increase_budget ───────────────────────────────────
        elif not p["auto_execute"] and p["action"] == "increase_budget":
            print(
                f"[Marketing] ◔ PENDING: increase_budget {p['campaign_name']} "
                f"PKR {p['current_budget_pkr']:.0f} → PKR {(p['new_budget_pkr'] or 0):.0f} (+{p['change_pct']:.0f}%)"
            )
            alerts.append(AgentAlert(
                level="info", agent="marketing_agent",
                message=(
                    f"PENDING APPROVAL: Budget increase +{p['change_pct']:.0f}% for '{p['campaign_name']}' "
                    f"(PKR {p['current_budget_pkr']:.0f} → PKR {(p['new_budget_pkr'] or 0):.0f}). Reason: {reason}"
                ),
                sku=p["sku"], created_at=now_iso,
            ))

        marketing_actions.append(action_rec)

    auto_exec = sum(1 for a in marketing_actions if a["auto_executed"])
    pending   = sum(1 for a in marketing_actions if not a["auto_executed"] and a["action"] != "hold")
    print(f"[Marketing] Done. {auto_exec} auto-executed, {pending} pending approval, {len(alerts)} alerts raised.")

    return {"marketing_actions": marketing_actions, "alerts": alerts}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_marketing_graph() -> StateGraph:
    graph = StateGraph(MarketingAgentState)

    graph.add_node("fetch_campaign_data",       fetch_campaign_data)
    graph.add_node("compute_marketing_plan",    compute_marketing_plan)
    graph.add_node("generate_marketing_copy",   generate_marketing_copy)
    graph.add_node("execute_marketing_actions", execute_marketing_actions)

    graph.add_edge(START,                        "fetch_campaign_data")
    graph.add_edge("fetch_campaign_data",        "compute_marketing_plan")
    graph.add_edge("compute_marketing_plan",     "generate_marketing_copy")
    graph.add_edge("generate_marketing_copy",    "execute_marketing_actions")
    graph.add_edge("execute_marketing_actions",  END)

    return graph.compile()


marketing_graph = build_marketing_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.marketing.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Marketing Agent Test Run")
        print("═" * 60 + "\n")

        mock_inventory = [
            {"sku": "FOS-001-S", "product_title": "Olive Cargo Pants", "variant_title": "Small",
             "current_stock": 18, "units_per_day": 1.8, "days_of_stock_remaining": 10.0, "urgency": "high"},
            {"sku": "FOS-002-M", "product_title": "Beige Linen Kurta", "variant_title": "Medium",
             "current_stock": 3,  "units_per_day": 0.5, "days_of_stock_remaining": 6.0,  "urgency": "critical"},
            {"sku": "FOS-003-S", "product_title": "Pink Chiffon Dupatta", "variant_title": "Free Size",
             "current_stock": 40, "units_per_day": 0.0, "days_of_stock_remaining": 999.0, "urgency": "normal"},
        ]
        mock_trends = [
            {"keyword": "cargo pants", "platform": "tiktok", "score": 0.82,
             "direction": "rising", "matched_sku": "FOS-001-S"},
        ]
        mock_pricing = [
            {"sku": "FOS-003-S", "variant_id": 123458, "current_price": 1499.0,
             "recommended_price": 899.0, "action": "clearance_code", "discount_pct": 40.0,
             "reason": "Dead stock — clearance."},
        ]

        initial_state: MarketingAgentState = {
            "brand_id":               os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":             os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":     mock_inventory,
            "trend_signals":          mock_trends,
            "pricing_recommendations":mock_pricing,
            "campaigns":              [],
            "computed_plan":          [],
            "raw_copy":               "",
            "marketing_actions":      [],
            "alerts":                 [],
        }

        result = await marketing_graph.ainvoke(initial_state)

        print("\n── MARKETING ACTIONS ──────────────────────────────────────────")
        for act in result["marketing_actions"]:
            status  = "🗸 AUTO" if act.get("auto_executed") else "◔ PENDING"
            sku_tag = f"[{act['sku']}]" if act.get("sku") else ""
            print(f"  {status}  {act['action'].upper():<18} {sku_tag}  {act['campaign_name']}  trigger={act['trigger']}")
            print(f"         PKR {act['current_budget_pkr']:.0f} → {act.get('new_budget_pkr') or '—'}  ({act['change_pct']:.0f}%)")
            print(f"         {act['reason']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()} [{alert.get('sku','—')}]: {alert['message'][:100]}...")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())