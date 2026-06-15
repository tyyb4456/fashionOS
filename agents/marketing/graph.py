"""
Marketing Agent — FashionOS Phase 2 Operations
===============================================
Reads live Meta ad campaign data and cross-references it with
trend_signals, inventory_snapshot, and pricing_recommendations
already in state. Pauses waste (out-of-stock/clearance SKUs),
decreases budget on low-performers, and queues budget increases
on trending SKUs for human approval.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  fetch_campaign_data      ← Node 1: ads-mcp → get_campaigns() + per-campaign
      │                               get_campaign_performance(7d).
      │                               Extracts SKU from campaign name.
      │                               Merges performance into compact payload.
      ▼
  load_domain_skill        ← Node 2: load_skill("fashion_marketing")
      │                               Budget rules, ROAS thresholds, PK ad benchmarks.
      ▼
  run_llm_analysis         ← Node 3: Structured LLM call.
      │                               Input: campaigns + inventory + trends + pricing.
      │                               Output: _CampaignDecision per campaign.
      ▼
  execute_marketing_actions← Node 4: Auto-execute: pause + decrease_budget ≤30%.
      │                               Pending approval: increase_budget, activate.
      │                               Writes marketing_actions + alerts.
      ▼
    END

Auto-execute thresholds:
  ✓ "hold"           → no API call needed
  ✓ "pause"          → auto-execute: SKU out of stock OR clearance
  ✓ "decrease_budget"≤ 30% → auto-execute: conservative, reversible
  ✗ "increase_budget"→ pending_approval: real money, human reviews
  ✗ "activate"       → pending_approval: human decides when to resume

Campaign ↔ SKU mapping:
  Convention: FashionOS_{SKU}_{desc}  e.g. FashionOS_FOS-001-S_OliveCargo
  Regex: r'FashionOS_([A-Z0-9\\-]+)_'
  Fallback: loose pattern r'\\b([A-Z]{2,5}-\\d{3}(?:-[A-Z]{1,2})?)\\b'
  If no match → matched_sku=None → action="hold" (conservative default)

Chaining:
  Reads: inventory_snapshot (Inventory), trend_signals (Trend),
         pricing_recommendations (Pricing)
  Runs: AFTER returns agent, BEFORE summarize
  Supervisor order: ... → returns → marketing → summarize

Standalone test:
  python -m agents.marketing.graph
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Optional
import operator

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import (
    AgentAlert,
    InventorySnapshot,
    MarketingAction,
    PricingRecommendation,
    TrendSignal,
)

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

ADS_MCP_URL = os.getenv("ADS_MCP_URL", "http://localhost:8004/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

# Ceiling for auto-execute budget decreases (inclusive)
AUTO_EXECUTE_DECREASE_CEILING_PCT = float(
    os.getenv("AUTO_EXECUTE_DECREASE_CEILING_PCT", "30.0")
)

# SKU extraction from campaign name
_SKU_CONVENTION_RE = re.compile(r"FashionOS[_\-]([A-Z0-9\-]+)[_\-]", re.IGNORECASE)
_SKU_FALLBACK_RE   = re.compile(r"\b([A-Z]{2,5}-\d{3}(?:-[A-Z]{1,2})?)\b")


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _CampaignDecision(BaseModel):
    """One budget/status decision per Meta campaign."""

    campaign_id:   str
    campaign_name: str
    matched_sku:   Optional[str] = Field(
        default=None,
        description=(
            "SKU extracted from campaign name. None if name doesn't follow the "
            "FashionOS_{SKU}_{desc} convention — conservative hold applied."
        ),
    )

    # Current state
    current_daily_budget_pkr: float
    current_status:           str   # "ACTIVE" | "PAUSED"
    has_daily_budget:         bool  # False = ad-set level budgets, can't change at campaign level

    # 7-day performance context
    roas_7d:     Optional[float] = None   # None if pixel not configured
    spend_7d_pkr: float           = 0.0
    ctr_7d:       float           = 0.0
    no_spend_data: bool           = False

    action: str = Field(
        description=(
            "One of: 'hold' | 'increase_budget' | 'decrease_budget' | 'pause' | 'activate'. "
            "'hold' = no change. 'pause' = stop spending. "
            "'activate' = resume paused campaign (always pending_approval)."
        )
    )
    new_daily_budget_pkr: Optional[float] = Field(
        default=None,
        description=(
            "Target daily budget in PKR. Set for increase/decrease actions. "
            "None for hold/pause/activate. "
            "Apply budget change ceiling: ±30% max per cycle."
        ),
    )
    change_pct: float = Field(
        default=0.0,
        description=(
            "% change from current budget. Positive = increase, negative = decrease. "
            "0 for hold/pause/activate."
        ),
    )

    auto_execute: bool = Field(
        description=(
            "True = execute via Meta API now. "
            "False = queue for human approval in dashboard. "
            "Rules: "
            "auto=True for: action='hold', action='pause', "
            "action='decrease_budget' with |change_pct| ≤ 30. "
            "auto=False for: action='increase_budget', action='activate'."
        )
    )
    reason: str = Field(
        description=(
            "1-2 sentence explanation referencing actual numbers. "
            "e.g. 'FOS-001-S is out of stock (3 units) — pausing campaign to stop driving "
            "traffic to an unavailable product.' "
            "OR 'Cargo pants trend score 0.82 (rising on TikTok PK) — "
            "increasing budget from PKR 500 to PKR 650 to capture peak demand.'"
        )
    )
    trigger: str = Field(
        description=(
            "What drove this decision. One of: "
            "'out_of_stock' | 'clearance' | 'trending' | 'organic_viral' | "
            "'low_roas' | 'healthy' | 'no_sku_match' | 'no_budget_control'"
        )
    )


class _MarketingAnalysis(BaseModel):
    """Complete structured output for one Marketing Agent run."""

    decisions: list[_CampaignDecision]
    summary: str = Field(
        description=(
            "2-3 sentence operational summary. "
            "Example: '6 campaigns analysed. 2 paused (out-of-stock SKUs: FOS-003, FOS-007). "
            "1 budget increase queued for approval (FOS-001 trending on TikTok). "
            "3 held — performance healthy.'"
        )
    )


# ── Subgraph state ─────────────────────────────────────────────────────────────

class MarketingAgentState(TypedDict):
    # From parent state (read-only context)
    brand_id:   str
    brand_name: str

    # From prior agents — already in state
    inventory_snapshot:      list[InventorySnapshot]
    trend_signals:           list[TrendSignal]
    pricing_recommendations: list[PricingRecommendation]

    # Node 1 output (internal scratch — LangGraph drops on merge)
    campaigns: list[dict]  # Campaigns enriched with performance + sku_match

    # Internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → operator.add merges safely with other agents
    marketing_actions: Annotated[list[MarketingAction], operator.add]
    alerts:            Annotated[list[AgentAlert],      operator.add]


# ── Helper: parse MCP results ──────────────────────────────────────────────────

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


# ── Helper: extract SKU from campaign name ─────────────────────────────────────

def _extract_sku(campaign_name: str) -> Optional[str]:
    """
    Extracts SKU from Meta campaign name.

    Priority:
    1. Exact convention: FashionOS_{SKU}_{desc}  → groups(1)
    2. Loose SKU pattern: uppercase + digits with hyphens
    3. None if no match
    """
    m = _SKU_CONVENTION_RE.search(campaign_name)
    if m:
        return m.group(1).upper()

    m = _SKU_FALLBACK_RE.search(campaign_name)
    if m:
        return m.group(1).upper()

    return None


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_campaign_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_campaign_data(state: MarketingAgentState) -> dict:
    """
    Fetches all active/paused campaigns from ads-mcp, then fetches 7-day
    performance data for each campaign that has a daily budget.

    Extracts SKU from campaign name using naming convention regex.
    Builds a compact payload (campaigns_with_perf) for Node 3 analysis.

    Uses tool_map.get() defensive pattern — if ads-mcp tools are missing
    (stale Docker image), the agent returns an empty campaign list and logs
    a warning. No crash.
    """
    client = MultiServerMCPClient(
        {"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    print(f"[Marketing] ads-mcp tools: {list(tool_map.keys())}")

    campaigns_raw: list[dict] = []

    # ── Fetch campaigns ────────────────────────────────────────────────────────
    if "get_campaigns" in tool_map:
        try:
            raw = await tool_map["get_campaigns"].ainvoke({"active_only": True, "brand_id": state["brand_id"]})
            result = _parse_mcp_result(raw)

            # Handle error response from server (missing credentials, etc.)
            if isinstance(result, list) and result and "error" in result[0]:
                print(f"[Marketing] ads-mcp error: {result[0]['error']}")
                return {"campaigns": []}

            campaigns_raw = result if isinstance(result, list) else []
        except Exception as exc:
            print(f"[Marketing] get_campaigns failed: {exc}")
            return {"campaigns": []}
    else:
        print("[Marketing] WARNING: get_campaigns not in tool_map — rebuild ads-mcp image")
        return {"campaigns": []}

    if not campaigns_raw:
        print("[Marketing] No active campaigns found.")
        return {"campaigns": []}

    # ── Fetch performance per campaign ─────────────────────────────────────────
    # Only fetch performance for campaigns that have a daily budget (CBO on)
    # and are in ACTIVE status — no point fetching perf for paused campaigns
    campaigns_enriched: list[dict] = []

    for camp in campaigns_raw:
        campaign_id = camp["campaign_id"]
        sku         = _extract_sku(camp["name"])

        perf = {}
        if camp.get("has_daily_budget") and camp.get("status") == "ACTIVE":
            if "get_campaign_performance" in tool_map:
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
            "campaign_id":            campaign_id,
            "name":                   camp["name"],
            "status":                 camp.get("status", "UNKNOWN"),
            "effective_status":       camp.get("effective_status", "UNKNOWN"),
            "daily_budget_pkr":       camp.get("daily_budget_pkr", 0.0),
            "has_daily_budget":       camp.get("has_daily_budget", False),
            "matched_sku":            sku,
            "follows_convention":     sku is not None,
            # Performance data (may be empty dict for paused campaigns)
            "roas_7d":                perf.get("purchase_roas"),
            "spend_7d_pkr":          perf.get("spend_pkr", 0.0),
            "ctr_7d":                 perf.get("ctr", 0.0),
            "no_spend_data":          perf.get("no_data", perf.get("no_spend_data", True)),
        })

    n_active    = sum(1 for c in campaigns_enriched if c["status"] == "ACTIVE")
    n_paused    = sum(1 for c in campaigns_enriched if c["status"] == "PAUSED")
    n_with_sku  = sum(1 for c in campaigns_enriched if c["matched_sku"])

    print(
        f"[Marketing] {len(campaigns_enriched)} campaigns: "
        f"{n_active} active, {n_paused} paused, {n_with_sku} SKU-matched."
    )

    return {"campaigns": campaigns_enriched}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: MarketingAgentState) -> dict:
    """Loads fashion_marketing skill: budget rules, ROAS thresholds, PK ad benchmarks."""
    skill = load_skill("fashion_marketing")
    print("[Marketing] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_llm_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_llm_analysis(state: MarketingAgentState) -> dict:
    """
    Single structured LLM call that produces a budget/status decision for every
    campaign, cross-referenced against inventory urgency, trend signals, and
    pricing actions already in state.

    Input payload (compact, token-efficient):
      - Per campaign: name, matched_sku, status, daily_budget, 7d ROAS/spend/CTR
      - Per matched SKU: inventory urgency, stock count, trend score/direction, pricing action

    Not a ReAct loop — all data is already in state. One structured call is sufficient.
    """
    campaigns = state.get("campaigns", [])

    if not campaigns:
        print("[Marketing] No campaigns to analyse.")
        empty = _MarketingAnalysis(
            decisions=[],
            summary="No Meta campaigns found. Create campaigns following the FashionOS_{SKU}_{desc} naming convention.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    # ── Build cross-reference lookups ──────────────────────────────────────────
    inv_by_sku: dict = {s["sku"]: s for s in state.get("inventory_snapshot", []) if s.get("sku")}
    pricing_by_sku: dict = {p["sku"]: p for p in state.get("pricing_recommendations", []) if p.get("sku")}
    trending_skus: dict = {
        sig["matched_sku"]: sig
        for sig in state.get("trend_signals", [])
        if sig.get("matched_sku") and sig.get("direction") in ("rising", "peaking")
    }

    # Compute store average velocity (for "organic viral" detection)
    velocities = [s.get("units_per_day", 0.0) for s in state.get("inventory_snapshot", [])]
    store_avg_velocity = sum(velocities) / len(velocities) if velocities else 0.0

    # ── Build compact campaign payload ─────────────────────────────────────────
    compact: list[dict] = []
    for camp in campaigns:
        sku  = camp.get("matched_sku")
        inv  = inv_by_sku.get(sku, {}) if sku else {}
        pri  = pricing_by_sku.get(sku, {}) if sku else {}
        trend= trending_skus.get(sku) if sku else None

        velocity    = inv.get("units_per_day", 0.0)
        is_trending = trend is not None
        is_viral    = velocity > (store_avg_velocity * 2) and store_avg_velocity > 0

        compact.append({
            # Campaign info
            "campaign_id":            camp["campaign_id"],
            "campaign_name":          camp["name"],
            "matched_sku":            sku,
            "follows_naming_convention": camp.get("follows_convention", False),
            "current_status":         camp["status"],
            "daily_budget_pkr":       camp["daily_budget_pkr"],
            "has_daily_budget":       camp["has_daily_budget"],
            # Performance (7d)
            "roas_7d":                camp.get("roas_7d"),
            "spend_7d_pkr":           camp.get("spend_7d_pkr", 0.0),
            "ctr_7d":                 camp.get("ctr_7d", 0.0),
            "no_spend_data":          camp.get("no_spend_data", True),
            # SKU context (from prior agents — empty if no match)
            "sku_current_stock":      inv.get("current_stock"),
            "sku_urgency":            inv.get("urgency"),
            "sku_units_per_day":      velocity,
            "sku_is_trending":        is_trending,
            "sku_trend_score":        round(trend["score"], 2) if trend else None,
            "sku_trend_direction":    trend.get("direction") if trend else None,
            "sku_pricing_action":     pri.get("action"),
            "sku_is_clearance":       pri.get("action") == "clearance_code",
            "sku_is_out_of_stock":    inv.get("current_stock", 999) < 5 if sku else False,
            "sku_is_organic_viral":   is_viral,
        })

    # ── Prompts ───────────────────────────────────────────────────────────────
    system_prompt = f"""You are the Marketing Agent for {state['brand_name']}, \
an autonomous AI fashion brand operating system.

{state['skill_content']}

## Decision framework — apply in THIS exact order per campaign:

### 1. No SKU match (follows_naming_convention = false OR matched_sku = null)
→ action = "hold", trigger = "no_sku_match"
→ auto_execute = True (hold = no change)
→ Reason: "Campaign name doesn't follow FashionOS_SKU_desc convention — cannot map to inventory. Hold until renamed."

### 2. No daily budget control (has_daily_budget = false)
→ action = "hold", trigger = "no_budget_control"
→ Cannot adjust budget at campaign level (ad-set budgets). Note this in reason.

### 3. Out of stock (sku_is_out_of_stock = true, stock < 5)
→ action = "pause", trigger = "out_of_stock", auto_execute = True
→ Stops burning ad spend on unavailable product immediately.

### 4. Clearance SKU (sku_is_clearance = true)
→ action = "pause", trigger = "clearance", auto_execute = True
→ Pricing Agent is clearing stock at deep discount; ads would waste money.

### 5. Trending SKU (sku_is_trending = true, trend_score ≥ 0.5)
→ If ROAS ≥ 2.5 or no_spend_data = true: action = "increase_budget" +25%
→ If ROAS < 2.5 but spend_7d > 0: action = "hold" (trending but ads inefficient, organic is better)
→ auto_execute = False (always — budget increase requires human approval)
→ new_daily_budget_pkr = current × 1.25, cap change at +30%

### 6. Organic viral (sku_is_organic_viral = true, selling without ads)
→ action = "decrease_budget" -30%, trigger = "organic_viral", auto_execute = True
→ Product is selling itself; ad spend is wasted at peak organic velocity.

### 7. Low ROAS (roas_7d < 1.5 AND spend_7d_pkr > PKR 500)
→ roas_7d < 0.8: action = "pause", trigger = "low_roas", auto_execute = True
→ 0.8 ≤ roas_7d < 1.5: action = "decrease_budget" -20%, trigger = "low_roas", auto_execute = True

### 8. Healthy (everything else, no signal)
→ action = "hold", trigger = "healthy", auto_execute = True

## Auto-execute rules (hard)
auto_execute = True ONLY for: "hold", "pause", "decrease_budget" with |change_pct| ≤ 30
auto_execute = False ALWAYS for: "increase_budget", "activate"

## Budget calculation
new_daily_budget_pkr must be rounded to nearest 50 PKR (e.g. 487 → 500, 512 → 500).
Minimum budget: PKR 200 (use "pause" instead of decreasing below this).
Maximum increase per cycle: +30%. Maximum decrease per cycle: -50%.

## Output requirement
Include EVERY campaign in decisions — even holds. No campaign may be omitted.
"""

    user_msg = (
        f"Meta ad campaigns for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Produce marketing decisions for all campaigns above."
    )

    structured_llm = model.with_structured_output(_MarketingAnalysis)
    analysis: _MarketingAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    auto_count    = sum(1 for d in analysis.decisions if d.auto_execute and d.action != "hold")
    pending_count = sum(1 for d in analysis.decisions if not d.auto_execute)
    pause_count   = sum(1 for d in analysis.decisions if d.action == "pause")

    print(
        f"[Marketing] Analysis complete. "
        f"{len(analysis.decisions)} decisions: "
        f"{pause_count} pause, {auto_count} auto-execute, {pending_count} pending. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_marketing_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_marketing_actions(state: MarketingAgentState) -> dict:
    """
    Executes auto-approved decisions via ads-mcp.
    Queues pending_approval decisions (increase_budget, activate) for dashboard.

    Auto-execute:
      pause            → calls pause_campaign()
      decrease_budget  → calls update_campaign_budget()

    All decisions (including holds and pending) are written to
    state.marketing_actions so the dashboard shows the full picture.
    """
    analysis = _MarketingAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    marketing_actions: list[MarketingAction] = []
    alerts:            list[AgentAlert]      = []

    # ── Open MCP connection ────────────────────────────────────────────────────
    client = MultiServerMCPClient(
        {"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    for d in analysis.decisions:
        # ── Build canonical MarketingAction ────────────────────────────────────
        action_rec = MarketingAction(
            sku          = d.matched_sku or "",
            campaign_id  = d.campaign_id,
            campaign_name= d.campaign_name,
            action       = d.action,
            reason       = d.reason,
            amount_delta = (
                (d.new_daily_budget_pkr - d.current_daily_budget_pkr)
                if d.new_daily_budget_pkr is not None else None
            ),
            auto_executed= d.auto_execute and d.action != "hold",
            trigger      = d.trigger,
        )

        # ── Auto-execute: pause ────────────────────────────────────────────────
        if d.auto_execute and d.action == "pause":
            if "pause_campaign" in tool_map:
                try:
                    await tool_map["pause_campaign"].ainvoke({
                        "campaign_id": d.campaign_id,
                        "reason":      f"[AUTO] {d.reason}",
                        "brand_id":    state["brand_id"]
                    })
                    print(f"[Marketing] ✓ Paused {d.campaign_id} ({d.campaign_name}): {d.trigger}")

                    alerts.append(AgentAlert(
                        level      = "warning",
                        agent      = "marketing_agent",
                        message    = (
                            f"AUTO-PAUSED campaign '{d.campaign_name}' "
                            f"(trigger: {d.trigger.replace('_', ' ')}). "
                            f"Reason: {d.reason}"
                        ),
                        sku        = d.matched_sku,
                        created_at = now_iso,
                    ))
                except Exception as exc:
                    print(f"[Marketing] ✗ Pause failed {d.campaign_id}: {exc}")
                    alerts.append(AgentAlert(
                        level      = "warning",
                        agent      = "marketing_agent",
                        message    = f"Pause FAILED for campaign '{d.campaign_name}': {exc}",
                        sku        = d.matched_sku,
                        created_at = now_iso,
                    ))
            else:
                print("[Marketing] WARNING: pause_campaign not in tool_map — rebuild ads-mcp image")

        # ── Auto-execute: decrease_budget ──────────────────────────────────────
        elif d.auto_execute and d.action == "decrease_budget" and d.new_daily_budget_pkr:
            if "update_campaign_budget" in tool_map:
                try:
                    await tool_map["update_campaign_budget"].ainvoke({
                        "campaign_id":          d.campaign_id,
                        "new_daily_budget_pkr": d.new_daily_budget_pkr,
                        "reason":               f"[AUTO] {d.reason}",
                        "brand_id":             state["brand_id"],
                    })
                    print(
                        f"[Marketing] ✓ Budget decreased {d.campaign_id}: "
                        f"PKR {d.current_daily_budget_pkr:.0f} → PKR {d.new_daily_budget_pkr:.0f} "
                        f"({d.change_pct:.0f}%)"
                    )

                    alerts.append(AgentAlert(
                        level      = "info",
                        agent      = "marketing_agent",
                        message    = (
                            f"AUTO: Budget decreased {abs(d.change_pct):.0f}% on "
                            f"'{d.campaign_name}' "
                            f"(PKR {d.current_daily_budget_pkr:.0f} → PKR {d.new_daily_budget_pkr:.0f}). "
                            f"Reason: {d.reason}"
                        ),
                        sku        = d.matched_sku,
                        created_at = now_iso,
                    ))
                except Exception as exc:
                    print(f"[Marketing] ✗ Budget update failed {d.campaign_id}: {exc}")

        # ── Pending approval: increase_budget ─────────────────────────────────
        elif not d.auto_execute and d.action == "increase_budget":
            print(
                f"[Marketing] ◔ PENDING: increase_budget {d.campaign_name} "
                f"PKR {d.current_daily_budget_pkr:.0f} → PKR {(d.new_daily_budget_pkr or 0):.0f} "
                f"(+{d.change_pct:.0f}%)"
            )
            alerts.append(AgentAlert(
                level      = "info",
                agent      = "marketing_agent",
                message    = (
                    f"PENDING APPROVAL: Budget increase +{d.change_pct:.0f}% for "
                    f"'{d.campaign_name}' "
                    f"(PKR {d.current_daily_budget_pkr:.0f} → PKR {(d.new_daily_budget_pkr or 0):.0f}). "
                    f"Reason: {d.reason}"
                ),
                sku        = d.matched_sku,
                created_at = now_iso,
            ))

        # ── Pending approval: activate ─────────────────────────────────────────
        elif not d.auto_execute and d.action == "activate":
            print(f"[Marketing] ◔ PENDING: activate {d.campaign_name}")
            alerts.append(AgentAlert(
                level      = "info",
                agent      = "marketing_agent",
                message    = (
                    f"PENDING APPROVAL: Activate campaign '{d.campaign_name}'. "
                    f"Reason: {d.reason}"
                ),
                sku        = d.matched_sku,
                created_at = now_iso,
            ))

        marketing_actions.append(action_rec)

    auto_exec  = sum(1 for a in marketing_actions if a.get("auto_executed"))
    pending    = sum(1 for a in marketing_actions if not a.get("auto_executed") and a.get("action") != "hold")

    print(
        f"[Marketing] Done. "
        f"{auto_exec} auto-executed, {pending} pending approval, "
        f"{len(alerts)} alerts raised."
    )

    return {
        "marketing_actions": marketing_actions,
        "alerts":            alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_marketing_graph() -> StateGraph:
    graph = StateGraph(MarketingAgentState)

    graph.add_node("fetch_campaign_data",      fetch_campaign_data)
    graph.add_node("load_domain_skill",        load_domain_skill)
    graph.add_node("run_llm_analysis",         run_llm_analysis)
    graph.add_node("execute_marketing_actions",execute_marketing_actions)

    graph.add_edge(START,                          "fetch_campaign_data")
    graph.add_edge("fetch_campaign_data",          "load_domain_skill")
    graph.add_edge("load_domain_skill",            "run_llm_analysis")
    graph.add_edge("run_llm_analysis",             "execute_marketing_actions")
    graph.add_edge("execute_marketing_actions",    END)

    return graph.compile()


marketing_graph = build_marketing_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.marketing.graph
# (requires ads-mcp on :8004 with META_ACCESS_TOKEN + META_AD_ACCOUNT_ID set)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Marketing Agent Test Run")
        print("═" * 60 + "\n")

        # Simulate prior agents having run
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
            "skill_content":          "",
            "raw_analysis":           "",
            "marketing_actions":      [],
            "alerts":                 [],
        }

        result = await marketing_graph.ainvoke(initial_state)

        print("\n── MARKETING ACTIONS ──────────────────────────────────────────")
        for act in result["marketing_actions"]:
            status = "🗸 AUTO" if act.get("auto_executed") else "◔ PENDING"
            sku_tag = f"[{act['sku']}]" if act.get("sku") else ""
            print(f"  {status}  {act['action'].upper():<18} {sku_tag}  {act['campaign_name']}")
            print(f"         {act['reason']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()} [{alert.get('sku','—')}]: {alert['message'][:100]}...")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())