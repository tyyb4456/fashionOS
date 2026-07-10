"""
Restock Agent — FashionOS Phase 2 Operations (deterministic-math rewrite)
===========================================================================
Reads inventory_snapshot and pricing_recommendations already in state.
ALL quantity math, supplier classification, dates, and cost estimates are
computed in plain Python (Node 2) — the LLM (Node 3) only writes natural-
language supplier messages, reasons, and a summary on top of numbers that
are already final.

Graph topology (4 nodes, sequential):

    START
      │
      ▼
  prepare_restock_data   ← Node 1: NO MCP — reads inventory_snapshot +
      │                             pricing_recommendations from state.
      │                             Filters to critical/high urgency.
      ▼
  compute_restock_plan   ← Node 2: PURE PYTHON. should_restock gate,
      │                             quantity formula, supplier classification,
      │                             stockout/deadline dates, cost estimates,
      │                             priority ordering. No LLM.
      ▼
  generate_copy          ← Node 3: THE ONLY LLM CALL. Given the fully
      │                             computed plan, writes per-SKU reasons,
      │                             per-SKU + consolidated-batch WhatsApp
      │                             messages (Urdu-English), and a summary.
      │                             Loads fashion_inventory skill inline.
      ▼
  execute_restock_actions← Node 4: Merges plan + copy, calls
      │                             create_restock_recommendation via
      │                             shopify-mcp, writes restock_recommendations
      │                             + alerts (overdue → critical alert).
      ▼
    END

DEDUP FIX (closes the flagged bug): Node 1 reads has_pending_restock /
pending_restock_note straight off inventory_snapshot — the Inventory Agent
already computes this every run (fresh DB check). No extra DB call needed
here; Restock just trusts state. Node 2 gates should_restock=False on it.

Quantity formula (Node 2):
  raw = ceil(units_per_day × (lead_time + 7)) - current_stock
  if raw <= 0: no restock
  raw = max(raw, 20)                      ← MOQ floor wins over the cap below
  cap = ceil(units_per_day × 60)          ← 2-month supply cap
  if cap >= 20: raw = min(raw, cap)

Supplier classification (Node 2, keyword-based, deterministic):
  china_import   → accessories/bags/shoes/jewelry keywords
  karachi_trader → basics/plain/staple keywords
  lahore_local   → default (Pakistani fashion: kurta, suit, lawn, co-ord, etc.)
  HARD RULE (enforced in code, not just prompted): china_import is never used
  for urgency="critical" — lead time too long. Falls back to lahore_local.

Chaining:
  Runs AFTER Inventory Agent (inventory_snapshot, incl. has_pending_restock)
  Runs AFTER Pricing Agent   (pricing_recommendations — used to skip clearance)

Standalone test:
  python -m agents.restock.graph
"""

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional
import operator

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agents.skills import load_skill
from agents.state import (
    AgentAlert,
    InventorySnapshot,
    PricingRecommendation,
    RestockRecommendation,
)
from response_schemas.restock_model import RestockPlanItem, RestockCopyPlan

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

SAFETY_BUFFER_DAYS = int(os.getenv("RESTOCK_SAFETY_BUFFER_DAYS", "7"))
MIN_ORDER_QTY       = int(os.getenv("RESTOCK_MIN_ORDER_QTY", "20"))
MAX_SUPPLY_DAYS     = int(os.getenv("RESTOCK_MAX_SUPPLY_DAYS", "60"))

SUPPLIER_LEAD_DAYS = {
    "lahore_local":   10,
    "karachi_trader":  7,
    "china_import":   32,
}

_CHINA_IMPORT_KEYWORDS = {
    "bag", "bags", "shoe", "shoes", "sneaker", "sneakers", "sandal", "sandals",
    "heel", "heels", "jewelry", "jewellery", "earring", "earrings", "necklace",
    "bracelet", "watch", "sunglasses", "belt", "clutch", "handbag", "purse",
    "accessory", "accessories",
}
_KARACHI_TRADER_KEYWORDS = {
    "basic", "basics", "plain", "tee", "t-shirt", "tshirt", "vest",
    "undershirt", "essential", "essentials", "staple",
}

# Order matters — first match wins.
_UNIT_COST_RULES: list[tuple[tuple[str, ...], float]] = [
    (("khaddar",), 1400.0),
    (("chiffon", "formal"), 2200.0),
    (("co-ord", "coord", "co ord"), 1800.0),
    (("lawn", "cotton"), 900.0),
    (("cargo", "bottom", "pant", "trouser", "palazzo"), 900.0),
    (("accessor", "bag", "jewelry", "jewellery", "clutch"), 500.0),
]


# ── Subgraph state ─────────────────────────────────────────────────────────────

class RestockAgentState(TypedDict):
    # From parent state (read-only context)
    brand_id:   str
    brand_name: str

    # Read from prior agents — already in state, no MCP fetch needed
    inventory_snapshot:      list[InventorySnapshot]
    pricing_recommendations: list[PricingRecommendation]

    # Node 1 output (internal scratch — LangGraph drops on merge)
    restock_candidates: list[dict]

    # Node 2 output (deterministic plan — internal scratch)
    computed_plan: list[dict]

    # LLM scratch
    raw_copy: str

    # Final outputs → merged into parent FashionOSState via operator.add
    restock_recommendations: Annotated[list[RestockRecommendation], operator.add]
    alerts:                  Annotated[list[AgentAlert],             operator.add]


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


# ── Helpers: deterministic classification / math ───────────────────────────────

def _classify_supplier_type(product_title: str, variant_title: str, urgency: str) -> str:
    text = f"{product_title} {variant_title}".lower()
    supplier = "lahore_local"
    if any(kw in text for kw in _CHINA_IMPORT_KEYWORDS):
        supplier = "china_import"
    elif any(kw in text for kw in _KARACHI_TRADER_KEYWORDS):
        supplier = "karachi_trader"
    # Hard rule, enforced here (not just prompted): china_import lead time is
    # too long for a critical stockout — always fall back to the fastest option.
    if supplier == "china_import" and urgency == "critical":
        supplier = "lahore_local"
    return supplier


def _estimate_unit_cost(product_title: str, variant_title: str) -> Optional[float]:
    text = f"{product_title} {variant_title}".lower()
    for keywords, cost in _UNIT_COST_RULES:
        if any(kw in text for kw in keywords):
            return cost
    return None


def _compute_quantity(units_per_day: float, lead_days: int, current_stock: int) -> int:
    if units_per_day <= 0:
        return 0
    raw = math.ceil(units_per_day * (lead_days + SAFETY_BUFFER_DAYS)) - current_stock
    if raw <= 0:
        return 0
    raw = max(raw, MIN_ORDER_QTY)          # MOQ floor
    cap = math.ceil(units_per_day * MAX_SUPPLY_DAYS)
    if cap >= MIN_ORDER_QTY:               # cap only applies above the MOQ
        raw = min(raw, cap)
    return raw


def _skip_item(c: dict, skip_reason: str, supplier_type: str = "lahore_local", lead_days: int = 0) -> dict:
    return RestockPlanItem(
        sku=c["sku"], product_title=c["product_title"], variant_title=c["variant_title"],
        should_restock=False, skip_reason=skip_reason,
        recommended_quantity=0, urgency=c["urgency"],
        days_of_stock_remaining=c["days_of_stock_remaining"], units_per_day=c["units_per_day"],
        current_stock=c["current_stock"], supplier_type=supplier_type,
        estimated_lead_days=lead_days, expected_stockout_date="", order_deadline="",
        is_overdue=False, estimated_unit_cost_pkr=None, estimated_total_cost_pkr=None,
        priority=999, status="pending_approval",
    ).model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — prepare_restock_data
# ══════════════════════════════════════════════════════════════════════════════

def prepare_restock_data(state: RestockAgentState) -> dict:
    """
    Reads inventory_snapshot + pricing_recommendations from state. No MCP call —
    data is already present from prior agents in this run.

    Filters to critical/high urgency SKUs and annotates each with:
      - pricing_action (skip if clearance_code)
      - zero_velocity (skip — dead stock, Pricing Agent handles it)
      - has_pending_restock / pending_restock_note, straight off the Inventory
        Agent's snapshot — this is the dedup fix. Inventory already re-checks
        in-flight restocks against the DB every run; Restock just trusts it
        instead of duplicating that DB call.
    """
    pricing_by_sku: dict[str, str] = {
        p["sku"]: p.get("action", "hold")
        for p in state.get("pricing_recommendations", [])
        if p.get("sku")
    }

    candidates: list[dict] = []

    for snap in state.get("inventory_snapshot", []):
        sku     = snap.get("sku", "")
        urgency = snap.get("urgency", "healthy")

        if urgency not in ("critical", "high"):
            continue

        velocity       = snap.get("units_per_day", 0.0)
        pricing_action = pricing_by_sku.get(sku, "hold")

        candidates.append({
            "sku":                     sku,
            "product_title":           snap.get("product_title", ""),
            "variant_title":           snap.get("variant_title", ""),
            "current_stock":           snap.get("current_stock", 0),
            "units_per_day":           velocity,
            "days_of_stock_remaining": snap.get("days_of_stock_remaining", 0.0),
            "urgency":                 urgency,
            "pricing_action":          pricing_action,
            "is_on_clearance":         pricing_action == "clearance_code",
            "zero_velocity":           velocity == 0.0,
            "has_pending_restock":     snap.get("has_pending_restock", False),
            "pending_restock_note":    snap.get("pending_restock_note"),
        })

    n_critical  = sum(1 for c in candidates if c["urgency"] == "critical")
    n_high      = sum(1 for c in candidates if c["urgency"] == "high")
    n_clearance = sum(1 for c in candidates if c["is_on_clearance"])
    n_pending   = sum(1 for c in candidates if c["has_pending_restock"])

    print(
        f"[Restock] {len(candidates)} candidates "
        f"({n_critical} critical, {n_high} high, {n_clearance} clearance, "
        f"{n_pending} already have a restock in flight)."
    )

    return {"restock_candidates": candidates}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — compute_restock_plan (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_restock_plan(state: RestockAgentState) -> dict:
    """
    should_restock gate, quantity formula, supplier classification, stockout/
    deadline dates, and cost estimates — all pure Python, all auditable. The
    LLM never sees these as something to calculate.
    """
    candidates = state.get("restock_candidates", [])
    today = date.today()

    plan: list[dict] = []

    for c in candidates:
        # ── should_restock gate, in priority order ──────────────────────────
        if c["has_pending_restock"]:
            plan.append(_skip_item(
                c, c.get("pending_restock_note") or "Restock already in flight for this SKU."
            ))
            continue

        if c["is_on_clearance"]:
            plan.append(_skip_item(
                c, "Pricing Agent is clearing this stock — restocking would be contradictory."
            ))
            continue

        if c["zero_velocity"]:
            plan.append(_skip_item(
                c, "Dead stock — zero velocity, no point ordering more."
            ))
            continue

        supplier_type = _classify_supplier_type(c["product_title"], c["variant_title"], c["urgency"])
        lead_days     = SUPPLIER_LEAD_DAYS[supplier_type]
        quantity      = _compute_quantity(c["units_per_day"], lead_days, c["current_stock"])

        if quantity <= 0:
            plan.append(_skip_item(
                c, "Current stock already covers the full lead time plus safety buffer.",
                supplier_type, lead_days,
            ))
            continue

        stockout_date  = today + timedelta(days=max(0, math.floor(c["days_of_stock_remaining"])))
        order_deadline = stockout_date - timedelta(days=lead_days)
        is_overdue     = order_deadline < today

        unit_cost  = _estimate_unit_cost(c["product_title"], c["variant_title"])
        total_cost = (unit_cost * quantity) if unit_cost is not None else None

        item = RestockPlanItem(
            sku=c["sku"], product_title=c["product_title"], variant_title=c["variant_title"],
            should_restock=True, skip_reason=None,
            recommended_quantity=quantity, urgency=c["urgency"],
            days_of_stock_remaining=c["days_of_stock_remaining"], units_per_day=c["units_per_day"],
            current_stock=c["current_stock"], supplier_type=supplier_type,
            estimated_lead_days=lead_days,
            expected_stockout_date=stockout_date.isoformat(), order_deadline=order_deadline.isoformat(),
            is_overdue=is_overdue, estimated_unit_cost_pkr=unit_cost, estimated_total_cost_pkr=total_cost,
            priority=0, status="pending_approval",
        )
        plan.append(item.model_dump())

    # ── Priority ordering: overdue first, then critical, then by stockout date ──
    orderable = [p for p in plan if p["should_restock"]]
    orderable.sort(key=lambda p: (
        0 if p["is_overdue"] else 1,
        0 if p["urgency"] == "critical" else 1,
        p["expected_stockout_date"],
    ))
    for i, p in enumerate(orderable):
        p["priority"] = i + 1

    n_overdue = sum(1 for p in orderable if p["is_overdue"])
    n_skipped = len(plan) - len(orderable)

    print(
        f"[Restock] Plan computed: {len(orderable)} to order "
        f"({n_overdue} overdue), {n_skipped} skipped."
    )

    return {"computed_plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — generate_copy (the ONLY LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_copy(state: RestockAgentState) -> dict:
    """
    Every number is already final. This call writes: per-SKU reason + WhatsApp
    message, per-supplier consolidated WhatsApp message, and an overall summary.
    """
    plan      = state.get("computed_plan", [])
    orderable = [p for p in plan if p["should_restock"]]

    if not orderable:
        empty = RestockCopyPlan(
            items=[], batches=[],
            summary=(
                "No SKUs require restocking this cycle. All inventory is healthy, "
                "on clearance, or already has a restock in flight."
            ),
        )
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_inventory")

    by_supplier: dict[str, list[dict]] = defaultdict(list)
    for p in orderable:
        by_supplier[p["supplier_type"]].append(p)

    batches_context = [
        {
            "supplier_type":       stype,
            "estimated_lead_days": items[0]["estimated_lead_days"],
            "skus":                [i["sku"] for i in items],
            "total_units":         sum(i["recommended_quantity"] for i in items),
        }
        for stype, items in by_supplier.items()
    ]

    system_prompt = f"""You are the Restock Agent for {state['brand_name']}, \
an autonomous AI fashion brand operating system.

{skill_content}

## Your task
Every number below — quantity, supplier, dates, cost — is FINAL, computed by \
deterministic Python logic. Do NOT recompute, second-guess, or contradict any \
number. Your only job is natural-language content:

1. Per SKU: a 1-2 sentence `reason` referencing the given numbers, and an \
   individual `supplier_message` WhatsApp text in Urdu-English mix.
2. Per supplier batch: one `consolidated_message` covering every SKU in that \
   batch — this is what actually gets sent, not the individual per-SKU ones.
3. A 2-3 sentence overall `summary` — lead with overdue/critical orders, \
   mention total units, supplier count, estimated spend.

## Supplier message style
- Natural Urdu-English mix (code-switching is normal in Pakistani business)
- Warm but businesslike — suppliers are partners, not vendors
- Be specific: SKU, product name, exact quantity, urgency, delivery deadline
- Always request price confirmation
- Individual messages under 150 words, consolidated batch messages under 300 words

## Output requirement
Include ALL items below — one entry per SKU, one per supplier batch. Never omit one.
"""

    user_msg = (
        f"Restock plan for {state['brand_name']} (today: {date.today().isoformat()}):\n\n"
        f"### Per-SKU plan\n```json\n{json.dumps(orderable, indent=2)}\n```\n\n"
        f"### Supplier batches\n```json\n{json.dumps(batches_context, indent=2)}\n```\n\n"
        "Write the reasons, supplier messages, consolidated batch messages, and summary."
    )

    structured_llm = model.with_structured_output(RestockCopyPlan)
    copy_plan: RestockCopyPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(
        f"[Restock] Copy generated for {len(copy_plan.items)} SKUs, "
        f"{len(copy_plan.batches)} supplier batches. Summary: {copy_plan.summary}"
    )

    return {"raw_copy": copy_plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_restock_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_restock_actions(state: RestockAgentState) -> dict:
    """
    Merges the deterministic plan (Node 2) with the LLM copy (Node 3), calls
    create_restock_recommendation via shopify-mcp for each order, and writes
    restock_recommendations + alerts. ALL orders are pending_approval — this
    agent never auto-orders. Overdue or critical orders raise a "critical" alert.
    """
    plan      = state.get("computed_plan", [])
    copy_plan = RestockCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    reason_by_sku  = {i.sku: i.reason for i in copy_plan.items}
    message_by_sku = {i.sku: i.supplier_message for i in copy_plan.items}
    consolidated_by_supplier = {b.supplier_type: b.consolidated_message for b in copy_plan.batches}

    orderable = sorted((p for p in plan if p["should_restock"]), key=lambda p: p["priority"])

    for p in plan:
        if not p["should_restock"]:
            print(f"[Restock] ◔ Skipped {p['sku']}: {p['skip_reason']}")

    if not orderable:
        print("[Restock] No restock orders to create this cycle.")
        return {"restock_recommendations": [], "alerts": []}

    client   = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    if "create_restock_recommendation" not in tool_map:
        print("[Restock] WARNING: 'create_restock_recommendation' not in tool_map — rebuild Docker image")

    restock_recommendations: list[RestockRecommendation] = []
    alerts:                  list[AgentAlert]             = []

    for p in orderable:
        sku     = p["sku"]
        reason  = reason_by_sku.get(
            sku, f"{p['urgency'].title()} urgency — {p['days_of_stock_remaining']:.1f} days of stock remaining."
        )
        message = message_by_sku.get(sku) or consolidated_by_supplier.get(p["supplier_type"], "")

        if "create_restock_recommendation" in tool_map:
            try:
                raw = await tool_map["create_restock_recommendation"].ainvoke({
                    "sku":                     sku,
                    "recommended_quantity":    p["recommended_quantity"],
                    "urgency":                 p["urgency"],
                    "days_of_stock_remaining": p["days_of_stock_remaining"],
                    "units_per_day":           p["units_per_day"],
                    "reason":                  reason,
                    "supplier_message":        message,
                    "brand_id": state["brand_id"],
                })
                _parse_mcp_result(raw)
            except Exception as exc:
                print(f"[Restock] ⚠ MCP call failed for {sku}: {exc}")

        rec = RestockRecommendation(
            sku                      = sku,
            recommended_quantity     = p["recommended_quantity"],
            urgency                  = p["urgency"],
            days_of_stock_remaining  = p["days_of_stock_remaining"],
            units_per_day            = p["units_per_day"],
            reason                   = reason,
            supplier_message         = message,
            status                   = "pending_approval",
            supplier_type            = p["supplier_type"],
            estimated_lead_days      = p["estimated_lead_days"],
            expected_stockout_date   = p["expected_stockout_date"],
            order_deadline           = p["order_deadline"],
            is_overdue               = p["is_overdue"],
            estimated_unit_cost_pkr  = p["estimated_unit_cost_pkr"],
            estimated_total_cost_pkr = p["estimated_total_cost_pkr"],
            priority                 = p["priority"],
        )
        restock_recommendations.append(rec)

        alert_level = "critical" if (p["urgency"] == "critical" or p["is_overdue"]) else "warning"
        overdue_tag = " [OVERDUE]" if p["is_overdue"] else ""
        alerts.append(AgentAlert(
            level      = alert_level,
            agent      = "restock_agent",
            message    = (
                f"RESTOCK PENDING APPROVAL{overdue_tag}: {sku} "
                f"({p['product_title']} / {p['variant_title']}). "
                f"{p['days_of_stock_remaining']:.1f} days remaining. "
                f"Order {p['recommended_quantity']} units via {p['supplier_type']} "
                f"({p['estimated_lead_days']}d lead). "
                f"Stockout: {p['expected_stockout_date']}. Order deadline: {p['order_deadline']}."
            ),
            sku        = sku,
            created_at = now_iso,
        ))

        print(
            f"[Restock] 🗸 Queued #{p['priority']} {sku}: "
            f"{p['recommended_quantity']} units | {p['urgency'].upper()}"
            f"{' | OVERDUE' if p['is_overdue'] else ''} | "
            f"{p['supplier_type']} ({p['estimated_lead_days']}d) | "
            f"stockout {p['expected_stockout_date']}"
        )

    print(
        f"[Restock] Done. {len(restock_recommendations)} orders pending approval, "
        f"{len(alerts)} alerts raised."
    )

    return {
        "restock_recommendations": restock_recommendations,
        "alerts":                  alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_restock_graph() -> StateGraph:
    graph = StateGraph(RestockAgentState)

    graph.add_node("prepare_restock_data",    prepare_restock_data)
    graph.add_node("compute_restock_plan",    compute_restock_plan)
    graph.add_node("generate_copy",           generate_copy)
    graph.add_node("execute_restock_actions", execute_restock_actions)

    graph.add_edge(START,                      "prepare_restock_data")
    graph.add_edge("prepare_restock_data",     "compute_restock_plan")
    graph.add_edge("compute_restock_plan",     "generate_copy")
    graph.add_edge("generate_copy",            "execute_restock_actions")
    graph.add_edge("execute_restock_actions",  END)

    return graph.compile()


restock_graph = build_restock_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.restock.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Restock Agent Test Run")
        print("═" * 60 + "\n")

        mock_inventory: list[InventorySnapshot] = [
            {
                "sku": "FOS-001-S", "product_title": "Olive Cargo Pants", "variant_title": "Small",
                "current_stock": 8, "units_per_day": 1.8, "days_of_stock_remaining": 4.4,
                "urgency": "critical", "has_pending_restock": False, "pending_restock_note": None,
            },
            {
                "sku": "FOS-001-M", "product_title": "Olive Cargo Pants", "variant_title": "Medium",
                "current_stock": 15, "units_per_day": 1.2, "days_of_stock_remaining": 12.5,
                "urgency": "high", "has_pending_restock": False, "pending_restock_note": None,
            },
            {
                "sku": "FOS-003-M", "product_title": "Pink Chiffon Dupatta", "variant_title": "Free Size",
                "current_stock": 5, "units_per_day": 0.0, "days_of_stock_remaining": 999.0,
                "urgency": "high", "has_pending_restock": False, "pending_restock_note": None,
            },
            # Demonstrates the dedup fix — critical urgency but already has a PO in flight.
            {
                "sku": "FOS-004-L", "product_title": "Beige Linen Co-ord Set", "variant_title": "Large",
                "current_stock": 3, "units_per_day": 1.5, "days_of_stock_remaining": 2.0,
                "urgency": "critical", "has_pending_restock": True,
                "pending_restock_note": "Restock already approved (40 units) — no new PO needed.",
            },
        ]

        mock_pricing: list[PricingRecommendation] = [
            {
                "sku": "FOS-001-S", "variant_id": 123456, "current_price": 2999.0,
                "recommended_price": 2999.0, "action": "hold", "discount_pct": 0.0,
                "reason": "High velocity — hold price.",
            },
            {
                "sku": "FOS-003-M", "variant_id": 123458, "current_price": 1499.0,
                "recommended_price": 899.0, "action": "clearance_code", "discount_pct": 40.0,
                "reason": "Dead stock 60+ days — clearance.",
            },
        ]

        initial_state: RestockAgentState = {
            "brand_id":                 os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":               os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":       mock_inventory,
            "pricing_recommendations":  mock_pricing,
            "restock_candidates":       [],
            "computed_plan":            [],
            "raw_copy":                 "",
            "restock_recommendations":  [],
            "alerts":                   [],
        }

        result = await restock_graph.ainvoke(initial_state)

        print("\n── RESTOCK PLAN (all candidates) ────────────────────────────")
        for p in result["computed_plan"]:
            if p["should_restock"]:
                print(
                    f"  ✓ #{p['priority']} {p['sku']:<12} {p['urgency'].upper():<10} "
                    f"order {p['recommended_quantity']:>4} via {p['supplier_type']:<14} "
                    f"({p['estimated_lead_days']}d) stockout {p['expected_stockout_date']} "
                    f"deadline {p['order_deadline']}{' OVERDUE' if p['is_overdue'] else ''}"
                )
            else:
                print(f"  ✗ {p['sku']:<12} skipped — {p['skip_reason']}")

        print("\n── SUPPLIER MESSAGES ──────────────────────────────────────────")
        for rec in result["restock_recommendations"]:
            print(f"\n  [{rec['sku']}]")
            print(f"  {rec['supplier_message']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert.get('sku', '—')}]"
            print(f"  {alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())