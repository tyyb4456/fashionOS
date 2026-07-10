"""
Returns Agent — FashionOS Phase 2 Operations (classification-to-LLM correction)
=================================================================================
Fetches the last 30 days of Shopify refund data, groups by SKU. Reason
classification of free-text customer complaints is an LLM job — not
keyword matching — because real return notes are messy (paraphrases,
compound reasons, Urdu-English code-switch). Return rate math, severity
thresholds, and fix_type lookup ARE genuinely deterministic and stay in
Python. Evidence paraphrasing and the recommended-fix write-up are prose
and stay with the LLM too.

Graph topology (5 nodes, sequential):

    START
      │
      ▼
  fetch_return_data       ← Node 1: shopify-mcp → get_returns(days=30).
      │                              Groups by SKU, enriches with sales
      │                              velocity from inventory_snapshot.
      ▼
  classify_return_reasons ← Node 2: THE FIRST LLM CALL. Classifies every
      │                              raw reason string into one of 7
      │                              categories from the fashion_returns
      │                              taxonomy — real language understanding,
      │                              not literal keyword matching. Own node,
      │                              separate from copy generation.
      ▼
  compute_return_plan     ← Node 3: PURE PYTHON. Aggregates the LLM's
      │                              classifications via Counter (counting
      │                              is arithmetic, not judgment) into
      │                              primary_reason + reason_breakdown.
      │                              Computes return_rate_pct, severity
      │                              (rate-based if known, else count-
      │                              based), and fix_type (fixed lookup —
      │                              the dashboard filters on this enum,
      │                              so it stays code-governed). Healthy
      │                              SKUs are dropped here.
      ▼
  generate_return_copy    ← Node 4: THE SECOND LLM CALL. Given the fully
      │                              computed plan, writes evidence
      │                              (paraphrase, never verbatim) and
      │                              recommended_fix (specific prose, not
      │                              a canned template) per flagged SKU,
      │                              plus an overall summary. Loads
      │                              fashion_returns skill inline.
      ▼
  write_state_outputs     ← Node 5: Merges Node 3's numbers + Node 4's
      │                              prose into AgentAlert (dashboard) +
      │                              ReturnInsightData (DB persistence).
      ▼
    END

Severity thresholds (Node 3, matches the skill exactly):
  rate-based (when estimated_30d_sales is known):
    >15% critical | 10-15% warning | 5-10% info | <5% healthy (dropped)
  count-based fallback (when sales data unavailable):
    >10 units critical | 6-10 warning | 3-5 info | <3 healthy (dropped)

fix_type mapping (Node 3, deterministic — the dashboard's Returns.jsx
filters/labels on this exact enum, so it must stay a controlled lookup):
    size_issue → update_size_guide | description_mismatch → update_photos
    quality_issue → quality_review | changed_mind → monitor
    late_delivery → update_description | duplicate_order → no_action
    other → monitor

Chaining:
  Runs AFTER Inventory Agent (inventory_snapshot, for sales velocity).

Standalone test:
  python -m agents.returns.graph
  (requires shopify-mcp on :8001 for live return data)
"""

import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Optional
import operator

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import AgentAlert, InventorySnapshot, ReturnInsightData
from response_schemas.return_model import (
    ReturnPlanItem,
    ReasonClassificationBatch,
    ReturnCopyPlan,
)

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL       = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
RETURNS_LOOKBACK_DAYS = int(os.getenv("RETURNS_LOOKBACK_DAYS", "30"))

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

CRITICAL_RATE_PCT = float(os.getenv("RETURNS_CRITICAL_RATE_PCT", "15.0"))
WARNING_RATE_PCT  = float(os.getenv("RETURNS_WARNING_RATE_PCT",  "10.0"))
INFO_RATE_PCT     = float(os.getenv("RETURNS_INFO_RATE_PCT",      "5.0"))
CRITICAL_UNITS    = int(os.getenv("RETURNS_CRITICAL_UNITS", "10"))
WARNING_UNITS     = int(os.getenv("RETURNS_WARNING_UNITS",   "6"))
INFO_UNITS        = int(os.getenv("RETURNS_INFO_UNITS",      "3"))

# Fixed lookup — NOT a judgment call. The dashboard (Returns.jsx fixTypeLabels)
# filters and labels on these exact enum values, so this stays code-governed.
_FIX_TYPE_BY_REASON = {
    "size_issue":           "update_size_guide",
    "description_mismatch": "update_photos",
    "quality_issue":        "quality_review",
    "changed_mind":          "monitor",
    "late_delivery":          "update_description",
    "duplicate_order":         "no_action",
    "other":                    "monitor",
}


# ── Subgraph state ─────────────────────────────────────────────────────────────

class ReturnsAgentState(TypedDict):
    brand_id:   str
    brand_name: str

    inventory_snapshot: list[InventorySnapshot]

    # Node 1 output
    raw_returns:    list[dict]
    returns_by_sku: list[dict]

    # Node 2 output (LLM scratch — flat classification list)
    raw_classifications: str

    # Node 3 output (deterministic plan — internal scratch)
    computed_plan: list[dict]

    # Node 4 output (LLM scratch)
    raw_copy: str

    # Final outputs → operator.add merges safely with other agents
    alerts:          Annotated[list[AgentAlert],        operator.add]
    return_insights: Annotated[list[ReturnInsightData], operator.add]


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


def _classify_severity(return_rate_pct: Optional[float], total_units_returned: int) -> str:
    """Rate-based when sales data is known, count-based fallback otherwise. Genuinely deterministic — stays Python."""
    if return_rate_pct is not None:
        if return_rate_pct > CRITICAL_RATE_PCT:
            return "critical"
        if return_rate_pct >= WARNING_RATE_PCT:
            return "warning"
        if return_rate_pct >= INFO_RATE_PCT:
            return "info"
        return "healthy"
    if total_units_returned > CRITICAL_UNITS:
        return "critical"
    if total_units_returned >= WARNING_UNITS:
        return "warning"
    if total_units_returned >= INFO_UNITS:
        return "info"
    return "healthy"


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_return_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_return_data(state: ReturnsAgentState) -> dict:
    """Fetches return records from shopify-mcp and groups them by SKU. Enriches with sales velocity from inventory_snapshot."""
    client   = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    raw_returns: list[dict] = []

    if "get_returns" in tool_map:
        try:
            raw = await tool_map["get_returns"].ainvoke({"days": RETURNS_LOOKBACK_DAYS, "brand_id": state["brand_id"]})
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
                "units_per_day":        0.0,
                "estimated_30d_sales":  None,
            }
        entry = by_sku[sku]
        entry["total_returns"]        += 1
        entry["total_units_returned"] += r.get("quantity", 1)
        reason = (r.get("return_reason") or "").strip()
        if reason:
            entry["reasons"].append(reason)

    inv_by_sku = {s["sku"]: s for s in state.get("inventory_snapshot", [])}
    for sku, data in by_sku.items():
        inv = inv_by_sku.get(sku, {})
        upd = inv.get("units_per_day", 0.0)
        if upd > 0:
            data["units_per_day"]       = round(upd, 2)
            data["estimated_30d_sales"] = max(1, round(upd * RETURNS_LOOKBACK_DAYS))

    returns_by_sku = list(by_sku.values())
    print(f"[Returns] Grouped into {len(returns_by_sku)} SKUs.")

    return {"raw_returns": raw_returns, "returns_by_sku": returns_by_sku}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — classify_return_reasons (the FIRST LLM call — its own node)
# ══════════════════════════════════════════════════════════════════════════════

async def classify_return_reasons(state: ReturnsAgentState) -> dict:
    """
    Classifies every raw customer return-reason string into one of the 7
    fashion_returns categories — via the LLM, not keyword matching. Real
    customer feedback needs actual language understanding: paraphrases,
    negation, compound complaints, and Urdu-English code-switching all break
    a keyword list. This node ONLY classifies — no evidence, no fix text,
    no summary. Counting the results is arithmetic and happens in Node 3.
    """
    rows = state.get("returns_by_sku", [])

    candidates: list[dict] = []
    for row in rows:
        for i, reason_text in enumerate(row.get("reasons", [])):
            candidates.append({"sku": row["sku"], "reason_index": i, "text": reason_text})

    if not candidates:
        print("[Returns] No reason text to classify.")
        return {"raw_classifications": ReasonClassificationBatch(classifications=[]).model_dump_json()}

    skill_content = load_skill("fashion_returns")

    system_prompt = f"""You are classifying customer return reasons for {state['brand_name']}, a Pakistani fashion brand.

{skill_content}

## Your task
For each (sku, reason_index, text) entry below, assign exactly ONE category from the taxonomy above:
size_issue | description_mismatch | quality_issue | changed_mind | late_delivery | duplicate_order | other

Use real understanding, not literal keyword matching:
- Handle paraphrases — "the fit was off" is size_issue even without the word "size"
- Handle Urdu-English code-switched text naturally
- Handle compound reasons — pick the DOMINANT complaint if a sentence mentions two things
- If genuinely ambiguous or unrelated to the taxonomy, use "other"

## Output requirement
Return exactly one classification per (sku, reason_index) pair below. Never omit one, never invent one that wasn't given.
"""

    user_msg = (
        f"Classify these {len(candidates)} return reasons:\n\n"
        f"```json\n{json.dumps(candidates, indent=2)}\n```"
    )

    structured_llm = model.with_structured_output(ReasonClassificationBatch)
    batch: ReasonClassificationBatch = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[Returns] Classified {len(batch.classifications)} / {len(candidates)} reasons.")

    return {"raw_classifications": batch.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — compute_return_plan (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_return_plan(state: ReturnsAgentState) -> dict:
    """
    Aggregates Node 2's per-reason classifications into per-SKU counts —
    this is counting, genuinely arithmetic, not judgment. Computes
    return_rate_pct, severity, and fix_type (fixed lookup). Healthy SKUs
    are dropped here, before the LLM ever sees them again.
    """
    rows = state.get("returns_by_sku", [])
    if not rows:
        print("[Returns] No return data — nothing to compute.")
        return {"computed_plan": []}

    batch = ReasonClassificationBatch.model_validate_json(state.get("raw_classifications") or '{"classifications": []}')
    category_by_key: dict[tuple[str, int], str] = {
        (c.sku, c.reason_index): c.category for c in batch.classifications
    }

    plan: list[dict] = []
    n_skipped_healthy = 0

    for row in rows:
        reasons: list[str] = row.get("reasons", [])

        if reasons:
            categories = [category_by_key.get((row["sku"], i), "other") for i in range(len(reasons))]
            counts = Counter(categories)
        else:
            # No free-text reasons at all for this SKU's returns — nothing to classify.
            counts = Counter({"other": row["total_returns"]})

        primary_reason   = counts.most_common(1)[0][0] if counts else "other"
        reason_breakdown = dict(counts)

        total_units          = row["total_units_returned"]
        estimated_30d_sales  = row.get("estimated_30d_sales")
        return_rate_pct = (
            round(total_units / estimated_30d_sales * 100, 1) if estimated_30d_sales else None
        )

        severity = _classify_severity(return_rate_pct, total_units)
        if severity == "healthy":
            n_skipped_healthy += 1
            continue

        fix_type = _FIX_TYPE_BY_REASON.get(primary_reason, "monitor")

        item = ReturnPlanItem(
            sku=row["sku"], product_title=row["product_title"],
            total_returns=row["total_returns"], total_units_returned=total_units,
            primary_reason=primary_reason, reason_breakdown=reason_breakdown,
            return_rate_pct=return_rate_pct, estimated_30d_sales=estimated_30d_sales,
            severity=severity, fix_type=fix_type,
            recommended_fix="",   # written by the LLM in Node 4 — this is prose, not a lookup
            sample_reasons=reasons[:5],
        )
        plan.append(item.model_dump())

    n_critical = sum(1 for p in plan if p["severity"] == "critical")
    n_warning  = sum(1 for p in plan if p["severity"] == "warning")
    n_info     = sum(1 for p in plan if p["severity"] == "info")

    print(
        f"[Returns] Plan computed: {len(plan)} SKUs flagged "
        f"({n_critical} critical, {n_warning} warning, {n_info} info), "
        f"{n_skipped_healthy} healthy skipped."
    )

    return {"computed_plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — generate_return_copy (the SECOND LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_return_copy(state: ReturnsAgentState) -> dict:
    """
    primary_reason, severity, fix_type, rate — all final. Writes evidence
    (paraphrase, never verbatim) and recommended_fix (specific prose, not
    a canned template) per flagged SKU, plus an overall summary.
    """
    plan = state.get("computed_plan", [])

    if not plan:
        empty = ReturnCopyPlan(
            items=[],
            summary=(
                f"No returns found in the last {RETURNS_LOOKBACK_DAYS} days, or all "
                "flagged SKUs are within healthy thresholds — no action needed."
            ),
        )
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_returns")

    compact = [
        {
            "sku": p["sku"], "product_title": p["product_title"],
            "primary_reason": p["primary_reason"], "reason_breakdown": p["reason_breakdown"],
            "total_units_returned": p["total_units_returned"], "return_rate_pct": p["return_rate_pct"],
            "severity": p["severity"], "fix_type": p["fix_type"], "sample_reasons": p["sample_reasons"],
        }
        for p in plan
    ]

    system_prompt = f"""You are the Returns Agent for {state['brand_name']}, an autonomous AI fashion brand operating system.

{skill_content}

## Your task
primary_reason, reason_breakdown, return_rate_pct, severity, and fix_type below are FINAL — \
already classified and computed. Do NOT recompute or contradict them. Write ONLY:

1. Per SKU: a 1-sentence `evidence` paraphrasing the pattern in `sample_reasons`. NEVER quote \
   verbatim — summarise in your own words.
2. Per SKU: a specific, actionable 1-2 sentence `recommended_fix` that references the actual \
   product name, primary_reason, and return count. The fix_type category is fixed (e.g. \
   "update_size_guide") — write the concrete action for THIS product, not a generic template.
3. A 2-3 sentence overall `summary` — lead with the most severe SKU.

## Output requirement
Include ALL SKUs listed below. Never omit one.
"""

    user_msg = (
        f"Flagged return patterns for {state['brand_name']} (last {RETURNS_LOOKBACK_DAYS} days):\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```"
    )

    structured_llm = model.with_structured_output(ReturnCopyPlan)
    copy_plan: ReturnCopyPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[Returns] Copy generated for {len(copy_plan.items)} SKUs. Summary: {copy_plan.summary}")

    return {"raw_copy": copy_plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: ReturnsAgentState) -> dict:
    """Merges Node 3's numbers with Node 4's prose into alerts + return_insights."""
    plan      = state.get("computed_plan", [])
    copy_plan = ReturnCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    evidence_by_sku = {c.sku: c.evidence for c in copy_plan.items}
    fix_by_sku      = {c.sku: c.recommended_fix for c in copy_plan.items}

    alerts:          list[AgentAlert]        = []
    return_insights: list[ReturnInsightData] = []

    for p in plan:
        evidence = evidence_by_sku.get(p["sku"]) or "Mixed feedback across returns — no dominant pattern identified."
        fix      = fix_by_sku.get(p["sku"]) or f"Review {p['product_title']} returns manually — no fix generated this cycle."
        rate_str = f" ({p['return_rate_pct']:.1f}% return rate)" if p["return_rate_pct"] is not None else ""

        alerts.append(AgentAlert(
            level      = p["severity"],
            agent      = "returns_agent",
            message    = (
                f"RETURNS {p['severity'].upper()}: {p['product_title']} ({p['sku']}) — "
                f"{p['total_units_returned']} units returned in {RETURNS_LOOKBACK_DAYS} days"
                f"{rate_str}. Primary reason: {p['primary_reason'].replace('_', ' ')}. "
                f"Evidence: {evidence} Fix ({p['fix_type']}): {fix}"
            ),
            sku        = p["sku"],
            created_at = now_iso,
        ))

        return_insights.append(ReturnInsightData(
            sku                   = p["sku"],
            product_title         = p["product_title"],
            total_returns         = p["total_returns"],
            total_units_returned  = p["total_units_returned"],
            primary_reason        = p["primary_reason"],
            reason_breakdown      = p["reason_breakdown"],
            return_rate_pct       = p["return_rate_pct"],
            estimated_30d_sales   = p["estimated_30d_sales"],
            severity              = p["severity"],
            recommended_fix       = fix,
            fix_type               = p["fix_type"],
            evidence                = evidence,
        ))

        print(f"[Returns] {p['severity'].upper()} [{p['sku']}]: {p['total_units_returned']} returns | {p['primary_reason']} | {p['fix_type']}")

    print(f"[Returns] Written {len(alerts)} alerts + {len(return_insights)} return insights to state.")

    return {"alerts": alerts, "return_insights": return_insights}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_returns_graph() -> StateGraph:
    graph = StateGraph(ReturnsAgentState)

    graph.add_node("fetch_return_data",       fetch_return_data)
    graph.add_node("classify_return_reasons", classify_return_reasons)
    graph.add_node("compute_return_plan",     compute_return_plan)
    graph.add_node("generate_return_copy",    generate_return_copy)
    graph.add_node("write_state_outputs",     write_state_outputs)

    graph.add_edge(START,                       "fetch_return_data")
    graph.add_edge("fetch_return_data",        "classify_return_reasons")
    graph.add_edge("classify_return_reasons",  "compute_return_plan")
    graph.add_edge("compute_return_plan",      "generate_return_copy")
    graph.add_edge("generate_return_copy",     "write_state_outputs")
    graph.add_edge("write_state_outputs",      END)

    return graph.compile()


returns_graph = build_returns_graph()


if __name__ == "__main__":
    import asyncio

    async def _test_run():
        initial_state: ReturnsAgentState = {
            "brand_id":            os.getenv("BRAND_ID",   "test-brand-001"),
            "brand_name":          os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":  [],
            "raw_returns":         [],
            "returns_by_sku":      [],
            "raw_classifications": "",
            "computed_plan":       [],
            "raw_copy":            "",
            "alerts":              [],
            "return_insights":     [],
        }
        result = await returns_graph.ainvoke(initial_state)
        for insight in result["return_insights"]:
            print(f"{insight['severity'].upper()} [{insight['sku']}] {insight['primary_reason']} — {insight['recommended_fix']}")

    asyncio.run(_test_run())