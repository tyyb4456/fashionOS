"""
Inventory Agent — FashionOS Phase 2 Operations (seasonal/trend-aware rewrite)
==============================================================================
Analyses live Shopify inventory with real velocity-trend detection, seasonal
demand awareness (Eid ramp-ups, summer/winter cycles), reorder-point math,
and dedup against restocks already in flight — instead of a flat velocity
average and an LLM re-guessing urgency every run.

Graph topology (4 nodes, sequential):

    START
      │
      ▼
  fetch_shopify_data         ← Node 1: list_products + calculate_sales_velocity
      │                                TWICE (7d responsive window, 30d baseline).
      │                                Also pulls in-flight restocks (DB) and
      │                                today's seasonal context (pure calendar math).
      ▼
  compute_snapshots          ← Node 2: PURE PYTHON. Velocity trend, confidence,
      │                                seasonal-adjusted forecast, reorder point,
      │                                pending-restock dedup, size-curve deviation.
      │                                No LLM involved — numbers are deterministic.
      ▼
  analyze_alerts_and_summary ← Node 3: THE ONLY LLM CALL. Loads the fashion_inventory
      │                                skill inline (was its own node — a sync dict
      │                                lookup doesn't need a graph hop). Given final
      │                                numbers, decides which snapshots need an alert
      │                                and writes it — softening alerts for SKUs with
      │                                a restock already in flight.
      ▼
  write_state_outputs        ← Node 4: merge Node 2's snapshots + Node 3's alerts.
      │
      ▼
    END

Why the LLM no longer computes urgency/trend/reorder-point:
  Numbers should be deterministic and auditable, not re-derived by a model
  every run. The LLM's value is judgment — which of these facts deserve an
  alert, how urgent the wording should be, whether context (seasonal ramp-up,
  pending restock) changes the story. Splitting it this way is also cheaper
  and faster (much smaller structured-output payload).

Standalone test:
  python -m agents.inventory.graph
"""

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Optional
import operator

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from langchain.chat_models import init_chat_model

from agents.skills import load_skill
from agents.seasonal import current_seasonal_context
from agents.state import AgentAlert, InventorySnapshot

from dotenv import load_dotenv
load_dotenv()

from response_schemas.inventory_model import SnapshotOut, InventoryAlertsAndSummary

# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
model = init_chat_model("google_genai:gemini-2.5-flash-lite")

DEFAULT_LEAD_TIME_DAYS = int(os.getenv("INVENTORY_DEFAULT_LEAD_DAYS", "10"))
SAFETY_BUFFER_DAYS     = int(os.getenv("INVENTORY_SAFETY_BUFFER_DAYS", "7"))
MIN_SIZE_SIGNAL_VPD    = float(os.getenv("INVENTORY_MIN_SIZE_SIGNAL_VPD", "0.5"))


# ── Subgraph State ─────────────────────────────────────────────────────────────

class InventoryAgentState(TypedDict):
    brand_id:   str
    brand_name: str

    products:       list[dict]
    sales_velocity: list[dict]   # kept for parent-state compatibility (= 7d velocity)

    # Node 1 output
    velocity_7d_raw:         list[dict]
    velocity_30d_raw:        list[dict]
    pending_restocks_by_sku: dict
    seasonal_ctx:            dict

    # Node 2 output (deterministic, pre-LLM)
    computed_snapshots: list[dict]

    # LLM scratch
    raw_analysis: str

    # Final outputs → merged into parent FashionOSState
    inventory_snapshot: Annotated[list[InventorySnapshot], operator.add]
    alerts:             Annotated[list[AgentAlert],        operator.add]


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


async def _fetch_pending_restocks(brand_id: str) -> dict[str, dict]:
    """
    Reads SKUs with a restock already pending_approval / approved / ordered,
    so this run doesn't re-raise a duplicate critical alert on something
    that's already been handled — this was the actual alert-fatigue bug.

    IMPORTANT: uses a fresh NullPool engine created inside THIS event loop.
    Reusing db.session.AsyncSessionLocal's module-level pooled engine here
    reproduces the Celery/Windows ProactorEventLoop bug (pooled connections
    bound to the wrong event loop).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from db import crud as db_crud

    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
    )
    engine  = create_async_engine(database_url, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    by_sku: dict[str, dict] = {}
    try:
        async with Session() as session:
            records = await db_crud.get_restocks_in_flight(session, brand_id=brand_id)
            for rec in records:
                by_sku.setdefault(rec.sku, {
                    "status":               rec.status,
                    "recommended_quantity": rec.recommended_quantity,
                })
    except Exception as exc:
        print(f"[Inventory] Pending-restock lookup failed (non-fatal): {exc}")
    finally:
        await engine.dispose()

    return by_sku


_SIZE_TOKENS = {
    "XL": {"xl", "xlarge", "x-large", "extra large", "extralarge"},
    "L":  {"l", "large"},
    "M":  {"m", "medium"},
    "S":  {"s", "small"},
}

def _infer_size_bucket(variant_title: str) -> Optional[str]:
    """Best-effort size bucket from a variant title. None if ambiguous (e.g. 'Free Size')."""
    tokens = set(re.split(r"[\s/,\-]+", (variant_title or "").strip().lower()))
    for bucket, keywords in _SIZE_TOKENS.items():
        if tokens & keywords:
            return bucket
    return None


def _classify_trend(v7: float, v30: float) -> str:
    if v30 <= 0 and v7 <= 0:
        return "no_movement"
    if v30 <= 0:
        return "new_item"
    ratio = v7 / v30
    if ratio >= 1.5:
        return "accelerating"
    if ratio <= 0.5:
        return "decelerating"
    return "stable"


def _classify_confidence(v30: float) -> str:
    total_30d_units = v30 * 30
    if total_30d_units >= 10:
        return "high"
    if total_30d_units >= 3:
        return "medium"
    return "low"


def _classify_urgency(days_remaining: float, v7: float, current_stock: int) -> str:
    if v7 == 0 and current_stock > 0:
        return "normal"   # dead stock, not a stockout risk — flagged separately
    if days_remaining < 7:
        return "critical"
    if days_remaining < 14:
        return "high"
    if days_remaining < 30:
        return "normal"
    return "healthy"


def _compute_size_curve_flags(rows: list[dict]) -> dict[str, str]:
    """SKU -> deviation note, only for products where L/XL are outselling S/M."""
    from collections import defaultdict
    by_product: dict[str, list[tuple[str, str, float]]] = defaultdict(list)

    for row in rows:
        bucket = _infer_size_bucket(row["variant_title"])
        if bucket:
            by_product[row["product_title"]].append((row["sku"], bucket, row["velocity_7d"]))

    flags: dict[str, str] = {}
    for product_title, entries in by_product.items():
        buckets_present = {b for _, b, _ in entries}
        if len(buckets_present) < 2:
            continue
        sm  = sum(v for _, b, v in entries if b in ("S", "M"))
        lxl = sum(v for _, b, v in entries if b in ("L", "XL"))
        if (sm + lxl) < MIN_SIZE_SIGNAL_VPD:
            continue
        if lxl > sm:
            note = (
                f"L/XL outselling S/M on '{product_title}' "
                f"({lxl:.1f}/day vs {sm:.1f}/day) — sizing may be running small."
            )
            for sku, _, _ in entries:
                flags[sku] = note

    return flags


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_shopify_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_shopify_data(state: InventoryAgentState) -> dict:
    """
    Fetches products + TWO velocity windows (7d responsive, 30d baseline) so
    trend direction is computed deterministically instead of guessed by the
    LLM. Also pulls in-flight restocks (DB) and today's seasonal context.
    """
    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    products_raw     = await tool_map["list_products"].ainvoke(
        {"limit": 250, "status": "active", "brand_id": state["brand_id"]}
    )
    velocity_7d_raw  = await tool_map["calculate_sales_velocity"].ainvoke(
        {"days": 7, "brand_id": state["brand_id"]}
    )
    velocity_30d_raw = await tool_map["calculate_sales_velocity"].ainvoke(
        {"days": 30, "brand_id": state["brand_id"]}
    )

    products     = _parse_mcp_result(products_raw)
    velocity_7d  = _parse_mcp_result(velocity_7d_raw)
    velocity_30d = _parse_mcp_result(velocity_30d_raw)

    pending_restocks = await _fetch_pending_restocks(state["brand_id"])
    seasonal_ctx     = current_seasonal_context()

    print(
        f"[Inventory] Fetched {len(products)} products, "
        f"{len(velocity_7d)} 7d-velocity / {len(velocity_30d)} 30d-velocity records, "
        f"{len(pending_restocks)} SKUs with restock already in flight. "
        f"Season: {seasonal_ctx['season_label']} (×{seasonal_ctx['demand_multiplier']})."
    )

    return {
        "products":                products,
        "sales_velocity":          velocity_7d,   # kept for parent-state compatibility
        "velocity_7d_raw":         velocity_7d,
        "velocity_30d_raw":        velocity_30d,
        "pending_restocks_by_sku": pending_restocks,
        "seasonal_ctx":            seasonal_ctx,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — compute_snapshots (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_snapshots(state: InventoryAgentState) -> dict:
    """
    Builds the full per-SKU snapshot in Python: velocity trend, confidence,
    seasonal-adjusted forecast, reorder point, pending-restock dedup, size-curve
    deviation. The LLM (Node 4) never touches these numbers.
    """
    v7_by_sku  = {v["sku"]: v["units_per_day"] for v in state.get("velocity_7d_raw", [])  if v.get("sku")}
    v30_by_sku = {v["sku"]: v["units_per_day"] for v in state.get("velocity_30d_raw", []) if v.get("sku")}
    pending    = state.get("pending_restocks_by_sku", {})
    season     = state.get("seasonal_ctx") or {"season_label": "off_season", "demand_multiplier": 1.0}
    multiplier = season.get("demand_multiplier", 1.0)
    season_label = season.get("season_label", "off_season")

    rows: list[dict] = []
    for product in state.get("products", []):
        for variant in product.get("variants", []):
            sku = (variant.get("sku") or "").strip()
            if not sku:
                continue
            if variant.get("inventory_management") != "shopify":
                continue

            rows.append({
                "sku":           sku,
                "product_title": product.get("title", ""),
                "variant_title": variant.get("title", ""),
                "current_stock": variant.get("inventory_quantity", 0),
                "velocity_7d":   v7_by_sku.get(sku, 0.0),
                "velocity_30d":  v30_by_sku.get(sku, 0.0),
            })

    if not rows:
        print("[Inventory] WARNING: No SKU data found. Check Shopify product setup.")
        return {"computed_snapshots": []}

    size_curve_flags = _compute_size_curve_flags(rows)

    snapshots: list[dict] = []
    for r in rows:
        sku, v7, v30, stock = r["sku"], r["velocity_7d"], r["velocity_30d"], r["current_stock"]

        days_unadjusted = round(stock / v7, 1) if v7 > 0 else 999.0
        effective_v7    = v7 * multiplier
        days_adjusted   = round(stock / effective_v7, 1) if effective_v7 > 0 else 999.0

        urgency = _classify_urgency(days_adjusted, v7, stock)

        pend = pending.get(sku)
        pending_note = (
            f"Restock already {pend['status'].replace('_', ' ')} "
            f"({pend['recommended_quantity']} units) — no new PO needed."
        ) if pend else None

        snap = SnapshotOut(
            sku=sku,
            product_title=r["product_title"],
            variant_title=r["variant_title"],
            current_stock=stock,
            units_per_day=round(v7, 2),
            days_of_stock_remaining=days_adjusted,
            urgency=urgency,
            velocity_7d=round(v7, 2),
            velocity_30d=round(v30, 2),
            velocity_trend=_classify_trend(v7, v30),
            velocity_confidence=_classify_confidence(v30),
            seasonal_multiplier_applied=multiplier,
            seasonal_context=season_label,
            days_of_stock_remaining_unadjusted=days_unadjusted,
            reorder_point_units=math.ceil(effective_v7 * (DEFAULT_LEAD_TIME_DAYS + SAFETY_BUFFER_DAYS)) if effective_v7 > 0 else 0,
            has_pending_restock=pend is not None,
            pending_restock_note=pending_note,
            size_curve_deviation=sku in size_curve_flags,
            size_curve_note=size_curve_flags.get(sku),
        )
        snapshots.append(snap.model_dump())

    n_critical = sum(1 for s in snapshots if s["urgency"] == "critical")
    n_accel    = sum(1 for s in snapshots if s["velocity_trend"] == "accelerating")
    print(
        f"[Inventory] Computed {len(snapshots)} snapshots — "
        f"{n_critical} critical, {n_accel} accelerating. "
        f"Season: {season_label} (×{multiplier})."
    )

    return {"computed_snapshots": snapshots}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — analyze_alerts_and_summary (the only LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def analyze_alerts_and_summary(state: InventoryAgentState) -> dict:
    """
    All numbers are already final (Node 2). This call is pure judgment: turn
    the pre-computed snapshots into specific, non-redundant alerts and a short
    executive summary — softening alerts for SKUs with a restock already in
    flight, and calling out seasonal ramp-ups explicitly.
    """
    skill_content = load_skill("fashion_inventory")   # NEW — inline, no dedicated node
    snapshots     = state.get("computed_snapshots", [])
    season        = state.get("seasonal_ctx", {})

    if not snapshots:
        empty = InventoryAlertsAndSummary(
            alerts=[],
            summary="No active SKUs found. Store may be empty or SKUs unassigned.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    compact = [
        {
            "sku":                     s["sku"],
            "product_title":           s["product_title"],
            "variant_title":           s["variant_title"],
            "current_stock":           s["current_stock"],
            "urgency":                 s["urgency"],
            "days_of_stock_remaining": s["days_of_stock_remaining"],
            "velocity_trend":          s["velocity_trend"],
            "velocity_confidence":     s["velocity_confidence"],
            "reorder_point_units":     s["reorder_point_units"],
            "has_pending_restock":     s["has_pending_restock"],
            "pending_restock_note":    s["pending_restock_note"],
            "size_curve_deviation":    s["size_curve_deviation"],
            "size_curve_note":         s["size_curve_note"],
        }
        for s in snapshots
    ]

    system_prompt = f"""You are the Inventory Agent for {state['brand_name']}, \
an autonomous AI fashion brand system.

{skill_content}

## Current seasonal context (already computed — trust it, don't recompute)
{json.dumps(season, indent=2)}

## Your task
Every snapshot below already has its final urgency, trend, and reorder point —
computed deterministically. Do NOT second-guess or recompute these numbers.
Your job is judgment: decide which snapshots deserve an alert, and write it well.

## Alert rules
1. CRITICAL/HIGH urgency + has_pending_restock=False → raise the alert normally.
2. CRITICAL/HIGH urgency + has_pending_restock=True, status "pending_approval"
   → still raise, but the message must say a restock is already awaiting
   approval — don't make it sound like a brand-new fire.
3. CRITICAL/HIGH urgency + has_pending_restock=True, status "approved"/"ordered"
   → downgrade to an "info" alert (or skip entirely if truly nothing to do).
4. Dead stock (velocity_trend="no_movement", current_stock > 0)
   → "warning" alert. Don't touch urgency — it's "normal" by design here.
5. size_curve_deviation=True → one "info" alert per affected product using the
   provided size_curve_note (don't invent your own numbers).
6. If the seasonal context shows an active ramp-up (demand_multiplier > 1.05)
   AND a snapshot is critical/high partly BECAUSE of that multiplier — say so
   explicitly in the message. This is the single most valuable alert this
   agent can raise: catching it early because of a known demand spike.
7. velocity_trend="accelerating" AND current_stock <= reorder_point_units,
   even if urgency is only "normal"/"healthy" → raise an "info"/"warning"
   alert flagging it as an early reorder signal.
8. Don't raise alerts for SKUs that are healthy with no signals.

## Output requirement
Only include snapshots that need attention. Most SKUs need no alert at all.
"""

    user_msg = (
        f"Pre-computed inventory snapshots for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Produce alerts and a summary from this data."
    )

    structured_llm = model.with_structured_output(InventoryAlertsAndSummary)
    analysis: InventoryAlertsAndSummary = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[Inventory] Analysis complete. {len(analysis.alerts)} alerts. Summary: {analysis.summary}")

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: InventoryAgentState) -> dict:
    """Merges the deterministic snapshots (Node 2) with the LLM's alerts (Node 4)."""
    analysis = InventoryAlertsAndSummary.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    inventory_snapshot: list[InventorySnapshot] = [
        InventorySnapshot(**s) for s in state.get("computed_snapshots", [])
    ]

    alerts: list[AgentAlert] = [
        AgentAlert(
            level=a.level,
            agent="inventory_agent",
            message=a.message,
            sku=a.sku,
            created_at=now_iso,
        )
        for a in analysis.alerts
    ]

    print(f"[Inventory] Written {len(inventory_snapshot)} snapshots and {len(alerts)} alerts to state.")

    return {
        "inventory_snapshot": inventory_snapshot,
        "alerts":             alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_inventory_graph() -> StateGraph:
    graph = StateGraph(InventoryAgentState)

    graph.add_node("fetch_shopify_data",         fetch_shopify_data)
    graph.add_node("compute_snapshots",          compute_snapshots)
    graph.add_node("analyze_alerts_and_summary", analyze_alerts_and_summary)
    graph.add_node("write_state_outputs",        write_state_outputs)

    graph.add_edge(START,                        "fetch_shopify_data")
    graph.add_edge("fetch_shopify_data",         "compute_snapshots")
    graph.add_edge("compute_snapshots",          "analyze_alerts_and_summary")
    graph.add_edge("analyze_alerts_and_summary", "write_state_outputs")
    graph.add_edge("write_state_outputs",        END)

    return graph.compile()


inventory_graph = build_inventory_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.inventory.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Inventory Agent Test Run")
        print("═" * 60 + "\n")

        initial_state: InventoryAgentState = {
            "brand_id":                os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":              os.getenv("BRAND_NAME", "TestBrand"),
            "products":                [],
            "sales_velocity":          [],
            "velocity_7d_raw":         [],
            "velocity_30d_raw":        [],
            "pending_restocks_by_sku": {},
            "seasonal_ctx":            {},
            "computed_snapshots":      [],
            "raw_analysis":            "",
            "inventory_snapshot":      [],
            "alerts":                  [],
        }

        result = await inventory_graph.ainvoke(initial_state)

        print("\n── INVENTORY SNAPSHOT ─────────────────────────────────────────")
        for snap in sorted(result["inventory_snapshot"], key=lambda s: s["days_of_stock_remaining"]):
            print(
                f"  {snap['sku']:<20} {snap['urgency'].upper():<10} "
                f"{snap['days_of_stock_remaining']:>6.1f}d (raw {snap['days_of_stock_remaining_unadjusted']:.1f}d) "
                f"trend={snap['velocity_trend']:<12} season={snap['seasonal_context']}"
            )

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert['sku']}]" if alert.get("sku") else ""
            print(f"{alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())