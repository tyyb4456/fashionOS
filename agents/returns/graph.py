"""
Returns Agent — FashionOS Phase 2 Operations
============================================
Fetches the last 30 days of Shopify refund data, clusters return reasons
by SKU, calculates return rates, and surfaces actionable fixes — update
size guide, reshoot photos, flag supplier quality issue, etc.

Graph topology  (4 nodes, sequential):

    START
      │
      ▼
  fetch_return_data      ← Node 1: shopify-mcp → get_returns(days=30).
      │                             Groups raw records by SKU.
      │                             Uses inventory_snapshot from state (if present)
      │                             to calculate return rates. No extra MCP call.
      ▼
  load_domain_skill      ← Node 2: load_skill("fashion_returns")
      │                             Reason taxonomy, rate thresholds, PK patterns.
      ▼
  run_gemini_analysis    ← Node 3: Structured LLM call.
      │                             Clusters free-text reasons → 6 categories.
      │                             Calculates return rate per SKU.
      │                             Produces one _ReturnPattern per affected SKU.
      ▼
  write_state_outputs    ← Node 4: Converts patterns → AgentAlerts.
      │                             critical/warning alerts for high-return SKUs.
      │                             info alerts for low-return SKUs worth monitoring.
      ▼
    END

Triggers:
  - refunds/create webhook  → real-time analysis when a refund happens
  - scheduled_run daily     → rolling 30-day review every day
  - manual                  → on-demand analysis

What it produces:
  state.alerts — one alert per SKU with returns:
    critical: return_rate > 15% or > 10 units returned
    warning:  return_rate 10-15% or 6-10 units returned
    info:     return_rate 5-10% or 3-5 units returned (monitor)
    (healthy SKUs produce no alert — noise reduction)

  Alert message includes:
    - SKU, product name
    - Total returns + return rate %
    - Primary reason category
    - Specific recommended fix (e.g. "Add cm measurements to size guide")
    - Evidence (paraphrased customer reason text)

No new MCP servers required — get_returns already in shopify-mcp.
No new DB tables required — alerts flow into existing alerts table via save_run().

Standalone test:
  python -m agents.returns.graph
"""

import json
import os
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
from agents.state import AgentAlert, InventorySnapshot

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL      = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
RETURNS_LOOKBACK_DAYS = int(os.getenv("RETURNS_LOOKBACK_DAYS", "30"))

model = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _ReturnPattern(BaseModel):
    """Analysis result for one SKU with returns."""

    sku:           str
    product_title: str

    total_returns:        int   = Field(ge=0, description="Number of return events in the lookback window.")
    total_units_returned: int   = Field(ge=0, description="Total units returned (may differ from events if multi-quantity).")

    primary_reason: str = Field(
        description=(
            "The dominant return reason category. Exactly one of:\n"
            "size_issue | description_mismatch | quality_issue | "
            "changed_mind | late_delivery | duplicate_order | other"
        )
    )
    reason_breakdown: dict = Field(
        description=(
            "Count per reason category for this SKU. "
            "e.g. {'size_issue': 4, 'description_mismatch': 1, 'changed_mind': 1}"
        )
    )
    evidence: str = Field(
        description=(
            "Paraphrase of the actual customer reason text. Do NOT quote verbatim — "
            "summarise the pattern in 1 sentence. "
            "Example: 'Most customers said the kurta runs small and the size chart was misleading.'"
        )
    )

    return_rate_pct: Optional[float] = Field(
        default=None,
        description=(
            "Return rate % = (total_units_returned / estimated_30d_sales) × 100. "
            "None if sales data unavailable."
        )
    )
    estimated_30d_sales: Optional[int] = Field(
        default=None,
        description="units_per_day × 30, from Inventory Agent data. None if unavailable."
    )

    severity: str = Field(
        description=(
            "Based on return_rate_pct if available, else absolute counts. "
            "critical: rate > 15% or > 10 units | "
            "warning: rate 10-15% or 6-10 units | "
            "info: rate 5-10% or 3-5 units | "
            "healthy: rate < 5% or < 3 units (skip — don't generate alerts for healthy)"
        )
    )

    recommended_fix: str = Field(
        description=(
            "Specific, actionable 1-2 sentence recommendation based on primary_reason.\n"
            "Must reference the actual product and reason. Not generic.\n"
            "Examples:\n"
            "size_issue → 'Add a size guide table with chest/waist/hip in cm and inches "
            "to the product page. Note whether this style runs true to size or slim fit.'\n"
            "quality_issue → 'Flag this batch to the supplier immediately and request "
            "a quality hold. Do not restock until the stitching issue is resolved.'\n"
            "description_mismatch → 'Reshoot in natural outdoor light and add "
            "a color accuracy note. Include exact fabric weight (gsm) in the description.'"
        )
    )

    fix_type: str = Field(
        description=(
            "Category for the dashboard fix queue. One of:\n"
            "update_size_guide | update_photos | update_description | "
            "quality_review | contact_supplier | monitor | no_action"
        )
    )


class _ReturnsAnalysis(BaseModel):
    patterns:              list[_ReturnPattern]
    total_returns_analyzed: int
    skus_analyzed:          int
    summary: str = Field(
        description=(
            "2-3 sentence operational summary.\n"
            "Example: '18 returns analyzed across 4 SKUs in the last 30 days. "
            "FOS-002 has a 22% return rate — size guide update is critical. "
            "FOS-001 returns are all changed_mind post-Eid — no product fix needed.'"
        )
    )


# ── Subgraph state ─────────────────────────────────────────────────────────────

class ReturnsAgentState(TypedDict):
    # From parent state
    brand_id:   str
    brand_name: str

    # From Inventory Agent (if it ran earlier in the same pipeline)
    # Used for return rate calculation. Empty list is handled gracefully.
    inventory_snapshot: list[InventorySnapshot]

    # Node 1 output (internal scratch)
    raw_returns:      list[dict]
    returns_by_sku:   list[dict]   # grouped + enriched with sales data

    # Internal scratch
    skill_content: str
    raw_analysis:  str

    # Final outputs → operator.add merges safely with other agents
    alerts: Annotated[list[AgentAlert], operator.add]


# ── Helper ─────────────────────────────────────────────────────────────────────

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


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_return_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_return_data(state: ReturnsAgentState) -> dict:
    """
    Fetches return records from shopify-mcp and groups them by SKU.

    Uses get_returns(days=RETURNS_LOOKBACK_DAYS) — default 30 days.
    Groups by SKU and calculates per-SKU stats.
    Enriches with sales velocity from inventory_snapshot if available
    (avoids a second MCP call — Inventory Agent already fetched this data).
    """
    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    raw_returns: list[dict] = []

    if "get_returns" in tool_map:
        try:
            raw = await tool_map["get_returns"].ainvoke(
                {"days": RETURNS_LOOKBACK_DAYS}
            )
            raw_returns = _parse_mcp_result(raw)
            if not isinstance(raw_returns, list):
                raw_returns = []
        except Exception as exc:
            print(f"[Returns] get_returns failed: {exc}")
    else:
        print("[Returns] WARNING: get_returns not in tool_map — rebuild shopify-mcp image")

    print(f"[Returns] Fetched {len(raw_returns)} return records from last {RETURNS_LOOKBACK_DAYS} days.")

    if not raw_returns:
        return {"raw_returns": [], "returns_by_sku": []}

    # ── Group by SKU ──────────────────────────────────────────────────────────
    by_sku: dict[str, dict] = {}
    for r in raw_returns:
        sku = (r.get("sku") or "NO_SKU").strip()
        if sku not in by_sku:
            by_sku[sku] = {
                "sku":                  sku,
                "product_title":        r.get("product_name", ""),
                "total_returns":        0,
                "total_units_returned": 0,
                "reasons":              [],
                "refunded_dates":       [],
                "units_per_day":        0.0,          # filled below
                "estimated_30d_sales":  None,
            }
        entry = by_sku[sku]
        entry["total_returns"]        += 1
        entry["total_units_returned"] += r.get("quantity", 1)

        reason = (r.get("return_reason") or "").strip()
        if reason:
            entry["reasons"].append(reason)

        date = r.get("refunded_at", "")
        if date:
            entry["refunded_dates"].append(date[:10])

    # ── Enrich with sales velocity from inventory_snapshot ─────────────────────
    inv_by_sku = {s["sku"]: s for s in state.get("inventory_snapshot", [])}
    for sku, data in by_sku.items():
        inv = inv_by_sku.get(sku, {})
        upd = inv.get("units_per_day", 0.0)
        if upd > 0:
            data["units_per_day"]       = round(upd, 2)
            data["estimated_30d_sales"] = max(1, round(upd * RETURNS_LOOKBACK_DAYS))

    returns_by_sku = list(by_sku.values())

    print(
        f"[Returns] Grouped into {len(returns_by_sku)} SKUs. "
        f"Top return count: "
        f"{max((d['total_returns'] for d in returns_by_sku), default=0)}"
    )

    return {
        "raw_returns":    raw_returns,
        "returns_by_sku": returns_by_sku,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: ReturnsAgentState) -> dict:
    """Loads fashion_returns skill — reason taxonomy, rate thresholds, PK patterns."""
    skill = load_skill("fashion_returns")
    print("[Returns] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_gemini_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_gemini_analysis(state: ReturnsAgentState) -> dict:
    """
    Single structured LLM call that:
    1. Clusters raw free-text return reasons into 6 categories
    2. Calculates return rate per SKU (using sales data if available)
    3. Assigns severity (critical/warning/info/healthy)
    4. Generates a specific, actionable recommended fix per SKU

    Only processes SKUs where severity != healthy — healthy returns produce no alert.
    """
    returns_by_sku = state.get("returns_by_sku", [])

    if not returns_by_sku:
        print("[Returns] No return data — skipping analysis.")
        empty = _ReturnsAnalysis(
            patterns               = [],
            total_returns_analyzed = 0,
            skus_analyzed          = 0,
            summary                = (
                f"No returns found in the last {RETURNS_LOOKBACK_DAYS} days. "
                "Return rate is healthy — no action needed."
            ),
        )
        return {"raw_analysis": empty.model_dump_json()}

    system_prompt = f"""You are the Returns Agent for {state['brand_name']}, \
an autonomous AI fashion brand operating system.

{state['skill_content']}

## Your task
Analyse return records per SKU. For each SKU:
1. Cluster the raw return reason texts into the 6 categories in the taxonomy
2. Identify the primary reason (highest count category)
3. Calculate return_rate_pct if estimated_30d_sales > 0
4. Assign severity using the thresholds
5. Generate one specific recommended fix

## Output rules
- Include ONLY SKUs where severity is critical, warning, or info.
- Skip "healthy" severity SKUs — don't waste the operator's attention on them.
- If a SKU has reasons=[] (no reason text provided by customer), classify as
  primary_reason="other" and recommend enabling required reason field on returns.
- recommended_fix must name the actual product and be specific — not generic advice.
- evidence must paraphrase, never directly quote customer text verbatim.
"""

    user_msg = (
        f"Return data for {state['brand_name']} "
        f"(last {RETURNS_LOOKBACK_DAYS} days):\n\n"
        f"```json\n{json.dumps(returns_by_sku, indent=2)}\n```\n\n"
        "Analyse each SKU and return the structured patterns."
    )

    structured_llm = model.with_structured_output(_ReturnsAnalysis)
    analysis: _ReturnsAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    critical = [p for p in analysis.patterns if p.severity == "critical"]
    warnings = [p for p in analysis.patterns if p.severity == "warning"]
    info     = [p for p in analysis.patterns if p.severity == "info"]

    print(
        f"[Returns] Analysis complete. "
        f"{len(analysis.patterns)} SKUs flagged: "
        f"{len(critical)} critical, {len(warnings)} warning, {len(info)} info. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: ReturnsAgentState) -> dict:
    """
    Converts _ReturnPattern objects → AgentAlerts.

    Alert level maps directly to severity:
      critical → "critical"
      warning  → "warning"
      info     → "info"
      healthy  → skipped (no alert generated)

    Message format includes: SKU, return count + rate, primary reason,
    recommended fix, and evidence summary.
    """
    analysis = _ReturnsAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()
    alerts:  list[AgentAlert] = []

    for p in analysis.patterns:
        if p.severity == "healthy":
            continue   # never alert on healthy SKUs

        # Build rate string if available
        rate_str = (
            f" ({p.return_rate_pct:.1f}% return rate)"
            if p.return_rate_pct is not None
            else ""
        )

        alerts.append(AgentAlert(
            level      = p.severity,
            agent      = "returns_agent",
            message    = (
                f"RETURNS {p.severity.upper()}: {p.product_title} ({p.sku}) — "
                f"{p.total_units_returned} units returned in {RETURNS_LOOKBACK_DAYS} days"
                f"{rate_str}. "
                f"Primary reason: {p.primary_reason.replace('_', ' ')}. "
                f"Evidence: {p.evidence} "
                f"Fix ({p.fix_type}): {p.recommended_fix}"
            ),
            sku        = p.sku,
            created_at = now_iso,
        ))

        print(
            f"[Returns] {p.severity.upper()} [{p.sku}]: "
            f"{p.total_units_returned} returns | "
            f"{p.primary_reason} | "
            f"{p.fix_type}"
        )

    if not alerts:
        print("[Returns] No actionable return issues found — all SKUs healthy.")

    print(f"[Returns] Written {len(alerts)} alerts to state.")

    return {"alerts": alerts}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_returns_graph() -> StateGraph:
    graph = StateGraph(ReturnsAgentState)

    graph.add_node("fetch_return_data",    fetch_return_data)
    graph.add_node("load_domain_skill",    load_domain_skill)
    graph.add_node("run_gemini_analysis",  run_gemini_analysis)
    graph.add_node("write_state_outputs",  write_state_outputs)

    graph.add_edge(START,                   "fetch_return_data")
    graph.add_edge("fetch_return_data",     "load_domain_skill")
    graph.add_edge("load_domain_skill",     "run_gemini_analysis")
    graph.add_edge("run_gemini_analysis",   "write_state_outputs")
    graph.add_edge("write_state_outputs",   END)

    return graph.compile()


returns_graph = build_returns_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.returns.graph
# (requires shopify-mcp on :8001 with a store that has some return history)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Returns Agent Test Run")
        print("═" * 60 + "\n")

        initial_state: ReturnsAgentState = {
            "brand_id":          os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":        os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":[],    # empty — standalone, no Inventory Agent ran first
            "raw_returns":       [],
            "returns_by_sku":    [],
            "skill_content":     "",
            "raw_analysis":      "",
            "alerts":            [],
        }

        result = await returns_graph.ainvoke(initial_state)

        print("\n── RETURN ALERTS ──────────────────────────────────────────────")
        if result["alerts"]:
            for alert in result["alerts"]:
                print(f"\n  {alert['level'].upper()} [{alert.get('sku', '—')}]")
                print(f"  {alert['message']}")
        else:
            print("  No actionable return issues — store return rates are healthy.")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())