"""
Pricing Agent — FashionOS Phase 2 Operations (deterministic-math rewrite)
============================================================================
Reads live prices, velocity, existing discounts, inventory urgency, and
trend signals from state. ALL decision logic — trending detection, markdown
ladder progression, psychological-99 price rounding, auto-execute gating,
margin estimation, discount code generation — is computed in plain Python
(Node 2). The LLM (Node 3) only writes per-SKU reasons and a summary on top
of numbers that are already final.

Graph topology (4 nodes, sequential):

    START
      │
      ▼
  fetch_pricing_data      ← Node 1: list_products + calculate_sales_velocity
      │                              + get_price_rules via shopify-mcp.
      ▼
  compute_pricing_plan    ← Node 2: PURE PYTHON. Trending detection, markdown
      │                              rung progression, psychological pricing,
      │                              margin estimate, auto_execute gating,
      │                              double-discount prevention, discount
      │                              code slug. No LLM.
      ▼
  generate_pricing_copy   ← Node 3: THE ONLY LLM CALL. Given the fully
      │                              computed plan, writes per-SKU reasons
      │                              and a summary. Loads fashion_pricing
      │                              skill inline. Only sees non-hold SKUs
      │                              (holds get a deterministic reason).
      ▼
  execute_pricing_actions ← Node 4: Executes auto_execute=True markdowns via
      │                              shopify-mcp. Everything else queued as
      │                              pending_approval. Writes
      │                              pricing_recommendations + alerts.
      ▼
    END

Decision framework (Node 2, in priority order):
  1. Trending (matched trend signal OR velocity ≥ 3x store average of
     selling SKUs) → hold, or increase 5-10% if margin ≥60% AND stock >20.
  2. Healthy non-trending (urgency healthy/normal, velocity>0) → hold.
  3. Slow-moving (velocity <0.3, not zero) → hold, UNLESS already on the
     markdown ladder (rung>0), in which case fall through to the ladder.
  4-6. Dead-stock ladder (zero velocity OR rung>0 carried over from #3):
     rung0→15% (auto-execute if stock>5), rung1→25% (pending), rung2+→
     clearance 35%/40% + discount code (pending, blocked if an active
     Shopify price rule already covers this product).
  7. Critical urgency, not trending, not on the ladder → hold (Restock
     Agent owns the urgency response, not Pricing).
  Fallback (anything uncovered, e.g. "high" urgency with normal velocity)
     → hold.

Psychological pricing: every markdown/clearance/increase price is rounded
to the nearest …99 tier (_round_psychological), then nudged up one tier if
rounding would push the actual discount more than 2pp past the target
(_apply_discount_tolerance) — matches the fashion_pricing skill's rule
exactly, just computed instead of prompted.

Chaining:
  Reads inventory_snapshot (Inventory Agent) and trend_signals (Trend Agent)
  from state — both already populated by the time Pricing runs.

Standalone test:
  python -m agents.pricing.graph
"""

import json
import math
import os
from datetime import date, datetime, timezone
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
    TrendSignal,
)
from response_schemas.pricing_model import PricingPlanItem, PricingCopyPlan

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")

model = init_chat_model("google_genai:gemini-2.5-flash-lite")

# Ceiling for auto-execute markdowns (inclusive). Rung-0 markdown is always
# 15% per the fashion_pricing skill's ladder, so this only matters if that
# base rate is ever tuned via env var without touching code.
AUTO_EXECUTE_MARKDOWN_CEILING_PCT = float(os.getenv("AUTO_EXECUTE_MARKDOWN_CEILING_PCT", "15.0"))

RUNG0_DISCOUNT_PCT      = float(os.getenv("PRICING_RUNG0_DISCOUNT_PCT", "15.0"))
RUNG1_DISCOUNT_PCT      = float(os.getenv("PRICING_RUNG1_DISCOUNT_PCT", "25.0"))
RUNG2_DISCOUNT_PCT      = float(os.getenv("PRICING_RUNG2_DISCOUNT_PCT", "35.0"))
RUNG2_CRITICAL_PCT      = float(os.getenv("PRICING_RUNG2_CRITICAL_PCT", "40.0"))
TRENDING_INCREASE_PCT   = float(os.getenv("PRICING_TRENDING_INCREASE_PCT", "7.5"))
MIN_STOCK_FOR_MARKDOWN  = int(os.getenv("PRICING_MIN_STOCK_FOR_MARKDOWN", "5"))
MIN_STOCK_FOR_INCREASE  = int(os.getenv("PRICING_MIN_STOCK_FOR_INCREASE", "20"))
MIN_MARGIN_FOR_INCREASE = float(os.getenv("PRICING_MIN_MARGIN_FOR_INCREASE", "60.0"))
TREND_VELOCITY_MULTIPLE = float(os.getenv("PRICING_TREND_VELOCITY_MULTIPLE", "3.0"))

_UNIT_COST_RULES: list[tuple[tuple[str, ...], float]] = [
    (("khaddar",), 1400.0),
    (("chiffon", "formal"), 2200.0),
    (("co-ord", "coord", "co ord"), 1800.0),
    (("lawn", "cotton"), 900.0),
    (("cargo", "bottom", "pant", "trouser", "palazzo"), 900.0),
    (("accessor", "bag", "jewelry", "jewellery", "clutch"), 500.0),
]

_HOLD_REASON_BY_TRIGGER = {
    "trending_hold":            "Trending, but doesn't clear the margin/stock bar for a markup — holding price.",
    "healthy":                  "Healthy velocity, no trend signal — no pricing action needed.",
    "slow_moving_watch":        "Velocity has slowed but not stalled — watching for 2 more cycles before considering a markdown.",
    "dead_stock_too_few_units": "Dead stock but too few units remaining to justify a markdown.",
    "double_discount_prevented":"Clearance blocked — an active Shopify price rule already discounts this product.",
    "critical_stockout_hold":   "Selling fast during a stockout risk window — holding price protects margin on high-demand inventory.",
    "critical_no_action":       "Critical stock risk but insufficient velocity/trend signal to justify a price move — Restock Agent owns this.",
}


# ── Subgraph state ─────────────────────────────────────────────────────────────

class PricingAgentState(TypedDict):
    # From parent state (read)
    brand_id:   str
    brand_name: str

    inventory_snapshot: list[InventorySnapshot]
    trend_signals:      list[TrendSignal]

    # Node 1 output
    products:              list[dict]
    sales_velocity:        list[dict]
    existing_price_rules:  list[dict]

    # Node 2 output (deterministic plan — internal scratch)
    computed_plan: list[dict]

    # LLM scratch
    raw_copy: str

    # Final outputs → merged into parent FashionOSState
    pricing_recommendations: Annotated[list[PricingRecommendation], operator.add]
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


# ── Helpers: deterministic math ────────────────────────────────────────────────

def _current_rung(current_price: float, compare_at_price: float) -> int:
    """
    Rung 0 = not marked down. Rung 1 = ~15% off. Rung 2 = ~25% off.
    Rung 3 = ~35%+ off (clearance territory). Read off Shopify's
    compare_at_price — the canonical source of truth for ladder state.
    """
    if compare_at_price <= 0 or compare_at_price <= current_price:
        return 0
    discount = (compare_at_price - current_price) / compare_at_price * 100
    if discount < 20:
        return 1
    if discount < 30:
        return 2
    return 3


def _round_psychological(price: float) -> float:
    """Round to the nearest …99 tier (2499, 2599, 2899…), ties rounding down."""
    if price <= 0:
        return 99.0
    bucket  = math.floor((price + 50) / 100)
    rounded = bucket * 100 - 1
    return float(max(rounded, 99.0))


def _apply_discount_tolerance(original_price: float, target_discount_pct: float, rounded_price: float) -> float:
    """If …99 rounding pushed the actual discount more than 2pp past the target, bump up one tier."""
    if original_price <= 0:
        return rounded_price
    actual_discount = (original_price - rounded_price) / original_price * 100
    if actual_discount > target_discount_pct + 2:
        return rounded_price + 100
    return rounded_price


def _price_for_target_discount(base_price: float, discount_pct: float) -> float:
    target  = base_price * (1 - discount_pct / 100)
    rounded = _round_psychological(target)
    return _apply_discount_tolerance(base_price, discount_pct, rounded)


def _estimate_unit_cost(product_title: str, variant_title: str) -> Optional[float]:
    text = f"{product_title} {variant_title}".lower()
    for keywords, cost in _UNIT_COST_RULES:
        if any(kw in text for kw in keywords):
            return cost
    return None


def _finalize(item: dict) -> dict:
    return PricingPlanItem(**item).model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_pricing_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_pricing_data(state: PricingAgentState) -> dict:
    """
    Fetches three datasets from shopify-mcp:
      1. list_products     → current prices + compare_at_prices (ladder state)
      2. calculate_sales_velocity → 14-day units/day per SKU
      3. get_price_rules   → existing discounts (double-discount prevention)

    Inventory snapshot and trend signals are already in state from prior
    agents — no need to re-fetch.
    """
    client   = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    products_raw = await tool_map["list_products"].ainvoke(
        {"limit": 250, "status": "active", "brand_id": state["brand_id"]}
    )
    velocity_raw = await tool_map["calculate_sales_velocity"].ainvoke(
        {"days": 14, "brand_id": state["brand_id"]}
    )
    rules_raw = await tool_map["get_price_rules"].ainvoke(
        {"active_only": True, "brand_id": state["brand_id"]}
    )

    products    = _parse_mcp_result(products_raw)
    velocity    = _parse_mcp_result(velocity_raw)
    price_rules = _parse_mcp_result(rules_raw)

    print(
        f"[Pricing] Fetched {len(products)} products, "
        f"{len(velocity)} velocity records, "
        f"{len(price_rules)} active price rules."
    )

    return {
        "products":             products,
        "sales_velocity":       velocity,
        "existing_price_rules": price_rules,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — compute_pricing_plan (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def compute_pricing_plan(state: PricingAgentState) -> dict:
    """
    Trending detection, markdown ladder progression, psychological pricing,
    margin estimation, auto_execute gating, double-discount prevention, and
    discount code generation — all pure Python. The LLM never touches these
    numbers; it only writes prose on top of them in Node 3.
    """
    velocity_by_sku: dict[str, float] = {
        v["sku"]: v["units_per_day"] for v in state.get("sales_velocity", []) if v.get("sku")
    }
    inv_by_sku: dict[str, InventorySnapshot] = {
        s["sku"]: s for s in state.get("inventory_snapshot", []) if s.get("sku")
    }
    trending_skus: set[str] = {
        sig["matched_sku"] for sig in state.get("trend_signals", [])
        if sig.get("matched_sku") and sig.get("direction") in ("rising", "peaking")
    }
    existing_rule_titles: list[str] = [
        r.get("title", "") for r in state.get("existing_price_rules", [])
    ]

    # Store average velocity, computed over SELLING SKUs only — including
    # dead stock (velocity 0) in the average would drag it down and make
    # "3x average" trivially easy to trigger for even modest sellers.
    selling_velocities = [v for v in velocity_by_sku.values() if v > 0]
    store_avg_velocity = sum(selling_velocities) / len(selling_velocities) if selling_velocities else 0.0

    year_month = date.today().strftime("%Y%m")
    plan: list[dict] = []

    for product in state.get("products", []):
        for variant in product.get("variants", []):
            sku = (variant.get("sku") or "").strip()
            if not sku:
                continue

            current_price    = variant.get("price", 0.0)
            compare_at_price = variant.get("compare_at_price", 0.0)
            velocity          = velocity_by_sku.get(sku, 0.0)
            inv_snap          = inv_by_sku.get(sku, {})
            urgency           = inv_snap.get("urgency", "unknown")
            current_stock     = inv_snap.get("current_stock", variant.get("inventory_quantity", 0))
            rung              = _current_rung(current_price, compare_at_price)
            zero_velocity     = velocity == 0.0
            is_trending       = sku in trending_skus or (
                store_avg_velocity > 0 and velocity >= store_avg_velocity * TREND_VELOCITY_MULTIPLE
            )

            unit_cost   = _estimate_unit_cost(product.get("title", ""), variant.get("title", ""))
            margin_pct  = (
                round((current_price - unit_cost) / current_price * 100, 1)
                if unit_cost is not None and current_price > 0 else None
            )

            base_item = dict(
                sku=sku, variant_id=variant.get("variant_id") or 0,
                product_title=product.get("title", ""), variant_title=variant.get("title", ""),
                current_price=current_price, compare_at_price=compare_at_price,
                current_markdown_rung=rung, markdown_rung=rung,
                action="hold", discount_pct=0.0, recommended_price=current_price,
                new_compare_at_price=None, auto_execute=True, trigger="healthy",
                estimated_unit_cost_pkr=unit_cost, estimated_margin_pct=margin_pct,
                suggested_discount_code=None,
            )

            # ── 1. Trending ──────────────────────────────────────────────────
            if is_trending:
                can_increase = (
                    margin_pct is not None
                    and margin_pct >= MIN_MARGIN_FOR_INCREASE
                    and current_stock > MIN_STOCK_FOR_INCREASE
                )
                if can_increase:
                    rec_price = _round_psychological(current_price * (1 + TRENDING_INCREASE_PCT / 100))
                    base_item.update(
                        action="increase", recommended_price=rec_price,
                        auto_execute=False, trigger="trending_increase",
                    )
                else:
                    base_item.update(action="hold", trigger="trending_hold")
                plan.append(_finalize(base_item))
                continue

            # ── 2. Healthy, non-trending ─────────────────────────────────────
            if urgency in ("healthy", "normal") and velocity > 0:
                base_item.update(action="hold", trigger="healthy")
                plan.append(_finalize(base_item))
                continue

            # ── 3. Slow-moving — hold unless already on the ladder ───────────
            if not zero_velocity and velocity < 0.3 and rung == 0:
                base_item.update(action="hold", trigger="slow_moving_watch")
                plan.append(_finalize(base_item))
                continue

            # ── 4/5/6. Dead-stock ladder (zero velocity, OR a slow-mover
            #           already on the ladder falls through here) ────────────
            if zero_velocity or rung > 0:
                base_price = compare_at_price if (rung > 0 and compare_at_price > 0) else current_price

                if rung == 0:
                    if current_stock > MIN_STOCK_FOR_MARKDOWN:
                        rec_price = _price_for_target_discount(base_price, RUNG0_DISCOUNT_PCT)
                        base_item.update(
                            action="markdown", discount_pct=RUNG0_DISCOUNT_PCT, recommended_price=rec_price,
                            new_compare_at_price=current_price, markdown_rung=1,
                            auto_execute=RUNG0_DISCOUNT_PCT <= AUTO_EXECUTE_MARKDOWN_CEILING_PCT,
                            trigger="dead_stock_first_markdown",
                        )
                    else:
                        base_item.update(action="hold", trigger="dead_stock_too_few_units")

                elif rung == 1:
                    rec_price = _price_for_target_discount(base_price, RUNG1_DISCOUNT_PCT)
                    base_item.update(
                        action="markdown", discount_pct=RUNG1_DISCOUNT_PCT, recommended_price=rec_price,
                        new_compare_at_price=compare_at_price, markdown_rung=2,
                        auto_execute=False, trigger="dead_stock_second_markdown",
                    )

                else:  # rung >= 2 → clearance
                    discount = RUNG2_CRITICAL_PCT if urgency == "critical" else RUNG2_DISCOUNT_PCT
                    title_lower = product.get("title", "").lower()
                    blocked = any(
                        rt and (rt.lower() in title_lower or title_lower in rt.lower())
                        for rt in existing_rule_titles
                    )
                    if blocked:
                        base_item.update(action="hold", trigger="double_discount_prevented")
                    else:
                        rec_price = _price_for_target_discount(base_price, discount)
                        slug = sku.upper().replace(" ", "").replace("_", "-")
                        base_item.update(
                            action="clearance_code", discount_pct=discount, recommended_price=rec_price,
                            new_compare_at_price=compare_at_price, markdown_rung=3,
                            auto_execute=False, trigger="dead_stock_clearance",
                            suggested_discount_code=f"CLEAR-{slug}-{year_month}",
                        )

                plan.append(_finalize(base_item))
                continue

            # ── 7. Critical urgency, not trending, not on the ladder ─────────
            if urgency == "critical":
                trigger = "critical_stockout_hold" if velocity > 2 else "critical_no_action"
                base_item.update(action="hold", trigger=trigger)
                plan.append(_finalize(base_item))
                continue

            # ── Fallback — anything uncovered (e.g. "high" urgency, decent velocity)
            base_item.update(action="hold", trigger="healthy")
            plan.append(_finalize(base_item))

    n_markdown  = sum(1 for p in plan if p["action"] == "markdown")
    n_clearance = sum(1 for p in plan if p["action"] == "clearance_code")
    n_increase  = sum(1 for p in plan if p["action"] == "increase")
    n_hold      = sum(1 for p in plan if p["action"] == "hold")

    print(
        f"[Pricing] Plan computed: {len(plan)} SKUs — "
        f"{n_markdown} markdown, {n_clearance} clearance, {n_increase} increase, {n_hold} hold."
    )

    return {"computed_plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — generate_pricing_copy (the ONLY LLM call)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_pricing_copy(state: PricingAgentState) -> dict:
    """
    Every number is already final. Writes a reason per non-hold SKU (holds
    get a deterministic reason from _HOLD_REASON_BY_TRIGGER in Node 4 — no
    need to spend tokens explaining a no-op) and an overall summary.
    """
    plan       = state.get("computed_plan", [])
    actionable = [p for p in plan if p["action"] != "hold"]

    if not actionable:
        empty = PricingCopyPlan(
            items=[],
            summary="All SKUs held this cycle — no markdowns, increases, or clearance actions triggered.",
        )
        return {"raw_copy": empty.model_dump_json()}

    skill_content = load_skill("fashion_pricing")

    compact = [
        {
            "sku": p["sku"], "product_title": p["product_title"], "variant_title": p["variant_title"],
            "action": p["action"], "discount_pct": p["discount_pct"],
            "current_price": p["current_price"], "recommended_price": p["recommended_price"],
            "trigger": p["trigger"], "markdown_rung": p["markdown_rung"],
            "estimated_margin_pct": p["estimated_margin_pct"],
            "suggested_discount_code": p["suggested_discount_code"],
        }
        for p in actionable
    ]

    system_prompt = f"""You are the Pricing Agent for {state['brand_name']}, an autonomous fashion brand AI.

{skill_content}

## Your task
Every number below — action, discount_pct, recommended_price, trigger — is FINAL, \
computed by deterministic Python logic. Do NOT recompute, second-guess, or contradict \
any number. Write ONLY:

1. Per SKU: a 1-2 sentence `reason` referencing the given action, price change, and \
   trigger context. Example: "FOS-001 has been dead stock for several cycles. First \
   markdown rung: PKR 2999 → PKR 2549 (15% off), auto-applying now."
2. A 2-3 sentence overall `summary` — lead with what's auto-executing this run, \
   mention pending approvals with the most urgent SKU.

## Output requirement
Include ALL SKUs listed below — one entry per SKU. Never omit one.
"""

    user_msg = (
        f"Pricing decisions for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Write the reasons and summary for the decisions above."
    )

    structured_llm = model.with_structured_output(PricingCopyPlan)
    copy_plan: PricingCopyPlan = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ])

    print(f"[Pricing] Copy generated for {len(copy_plan.items)} SKUs. Summary: {copy_plan.summary}")

    return {"raw_copy": copy_plan.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — execute_pricing_actions
# ══════════════════════════════════════════════════════════════════════════════

async def execute_pricing_actions(state: PricingAgentState) -> dict:
    """
    Executes auto_execute=True markdowns via shopify-mcp. Everything else
    (increase, second markdown, clearance) is queued as pending_approval.
    Every SKU in the plan — including holds — is written to
    pricing_recommendations, since downstream agents (Restock, Content,
    Marketing) all read it for full-catalog context.
    """
    plan      = state.get("computed_plan", [])
    copy_plan = PricingCopyPlan.model_validate_json(state["raw_copy"])
    now_iso   = datetime.now(timezone.utc).isoformat()

    reason_by_sku = {c.sku: c.reason for c in copy_plan.items}

    client   = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    tools    = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    pricing_recommendations: list[PricingRecommendation] = []
    alerts:                  list[AgentAlert]             = []
    auto_count = 0
    fail_count = 0

    for item in plan:
        reason = reason_by_sku.get(item["sku"]) or _HOLD_REASON_BY_TRIGGER.get(
            item["trigger"], "No price action needed this cycle."
        )

        rec = PricingRecommendation(
            sku               = item["sku"],
            variant_id        = item["variant_id"],
            current_price     = item["current_price"],
            recommended_price = item["recommended_price"],
            action            = item["action"],
            discount_pct      = item["discount_pct"],
            reason            = reason,
            auto_executed              = item["auto_execute"],
            trigger                    = item["trigger"],
            markdown_rung               = item["markdown_rung"],
            estimated_unit_cost_pkr     = item["estimated_unit_cost_pkr"],
            estimated_margin_pct        = item["estimated_margin_pct"],
            suggested_discount_code     = item["suggested_discount_code"],
            new_compare_at_price        = item["new_compare_at_price"],
        )

        # ── Auto-execute path ────────────────────────────────────────────────
        if item["auto_execute"] and item["action"] == "markdown":
            try:
                await tool_map["update_product_price"].ainvoke({
                    "variant_id":       item["variant_id"],
                    "new_price":        item["recommended_price"],
                    "compare_at_price": item["new_compare_at_price"],
                    "reason":           f"[AUTO] {reason}",
                    "brand_id":         state["brand_id"],
                })
                auto_count += 1
                print(
                    f"[Pricing] 🗸 Auto-executed markdown on {item['sku']}: "
                    f"PKR {item['current_price']} → PKR {item['recommended_price']} "
                    f"({item['discount_pct']:.0f}% off)"
                )
                alerts.append(AgentAlert(
                    level      = "info",
                    agent      = "pricing_agent",
                    message    = (
                        f"Auto-executed {item['discount_pct']:.0f}% markdown on {item['sku']} "
                        f"({item['product_title']} / {item['variant_title']}): "
                        f"PKR {item['current_price']:.0f} → PKR {item['recommended_price']:.0f}."
                    ),
                    sku        = item["sku"],
                    created_at = now_iso,
                ))
            except Exception as exc:
                fail_count += 1
                print(f"[Pricing] 🗴 Failed to execute on {item['sku']}: {exc}")
                alerts.append(AgentAlert(
                    level      = "warning",
                    agent      = "pricing_agent",
                    message    = f"Price update FAILED for {item['sku']}: {exc}",
                    sku        = item["sku"],
                    created_at = now_iso,
                ))

        # ── Pending-approval path ──────────────────────────────────────────
        elif not item["auto_execute"] and item["action"] != "hold":
            print(
                f"[Pricing] ◔ Queued for approval: {item['action']} on {item['sku']} "
                f"({item['discount_pct']:.0f}% → PKR {item['recommended_price']:.0f})"
            )

            if item["action"] in ("markdown", "clearance_code") and item["discount_pct"] > 15:
                code_tag = f" Code: {item['suggested_discount_code']}." if item["suggested_discount_code"] else ""
                alerts.append(AgentAlert(
                    level      = "warning",
                    agent      = "pricing_agent",
                    message    = (
                        f"PENDING APPROVAL: {item['discount_pct']:.0f}% {item['action']} "
                        f"on {item['sku']} ({item['product_title']} / {item['variant_title']}). "
                        f"Recommended price: PKR {item['recommended_price']:.0f}.{code_tag} "
                        f"Reason: {reason}"
                    ),
                    sku        = item["sku"],
                    created_at = now_iso,
                ))

            if item["action"] == "increase":
                alerts.append(AgentAlert(
                    level      = "info",
                    agent      = "pricing_agent",
                    message    = (
                        f"PENDING APPROVAL: Price increase on {item['sku']} "
                        f"({item['product_title']}). "
                        f"Suggested: PKR {item['current_price']:.0f} → "
                        f"PKR {item['recommended_price']:.0f}. Reason: {reason}"
                    ),
                    sku        = item["sku"],
                    created_at = now_iso,
                ))

        pricing_recommendations.append(rec)

    pending = len([p for p in plan if not p["auto_execute"] and p["action"] != "hold"])
    print(f"[Pricing] Done. {auto_count} auto-executed, {fail_count} failed, {pending} pending.")

    return {
        "pricing_recommendations": pricing_recommendations,
        "alerts":                  alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_pricing_graph() -> StateGraph:
    graph = StateGraph(PricingAgentState)

    graph.add_node("fetch_pricing_data",      fetch_pricing_data)
    graph.add_node("compute_pricing_plan",    compute_pricing_plan)
    graph.add_node("generate_pricing_copy",   generate_pricing_copy)
    graph.add_node("execute_pricing_actions", execute_pricing_actions)

    graph.add_edge(START,                     "fetch_pricing_data")
    graph.add_edge("fetch_pricing_data",      "compute_pricing_plan")
    graph.add_edge("compute_pricing_plan",    "generate_pricing_copy")
    graph.add_edge("generate_pricing_copy",   "execute_pricing_actions")
    graph.add_edge("execute_pricing_actions", END)

    return graph.compile()


pricing_graph = build_pricing_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.pricing.graph
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Pricing Agent Test Run")
        print("═" * 60 + "\n")

        mock_products = [
            {
                "product_id": 1, "title": "Olive Cargo Pants", "status": "active", "tags": "",
                "variants": [{
                    "variant_id": 111, "sku": "FOS-001-S", "title": "Small",
                    "price": 2999.0, "compare_at_price": 0.0, "inventory_quantity": 40,
                }],
            },
            {
                "product_id": 2, "title": "Pink Chiffon Dupatta", "status": "active", "tags": "",
                "variants": [{
                    "variant_id": 222, "sku": "FOS-003-M", "title": "Free Size",
                    "price": 1499.0, "compare_at_price": 0.0, "inventory_quantity": 22,
                }],
            },
            {
                "product_id": 3, "title": "Beige Linen Co-ord Set", "status": "active", "tags": "",
                "variants": [{
                    "variant_id": 333, "sku": "FOS-005-L", "title": "Large",
                    "price": 2549.0, "compare_at_price": 2999.0, "inventory_quantity": 14,
                }],
            },
        ]
        mock_velocity = [
            {"sku": "FOS-001-S", "units_per_day": 1.8},
            {"sku": "FOS-003-M", "units_per_day": 0.0},
            {"sku": "FOS-005-L", "units_per_day": 0.1},
        ]
        mock_inventory: list[InventorySnapshot] = [
            {"sku": "FOS-001-S", "product_title": "Olive Cargo Pants", "variant_title": "Small",
             "current_stock": 40, "units_per_day": 1.8, "days_of_stock_remaining": 22.2, "urgency": "normal"},
            {"sku": "FOS-003-M", "product_title": "Pink Chiffon Dupatta", "variant_title": "Free Size",
             "current_stock": 22, "units_per_day": 0.0, "days_of_stock_remaining": 999.0, "urgency": "normal"},
            {"sku": "FOS-005-L", "product_title": "Beige Linen Co-ord Set", "variant_title": "Large",
             "current_stock": 14, "units_per_day": 0.1, "days_of_stock_remaining": 140.0, "urgency": "normal"},
        ]
        mock_trends: list[TrendSignal] = [
            {"keyword": "cargo pants", "platform": "tiktok", "score": 0.82,
             "direction": "rising", "matched_sku": "FOS-001-S"},
        ]

        initial_state: PricingAgentState = {
            "brand_id":               os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":             os.getenv("BRAND_NAME", "TestBrand"),
            "inventory_snapshot":     mock_inventory,
            "trend_signals":          mock_trends,
            "products":               mock_products,
            "sales_velocity":         mock_velocity,
            "existing_price_rules":   [],
            "computed_plan":          [],
            "raw_copy":               "",
            "pricing_recommendations":[],
            "alerts":                 [],
        }

        result = await pricing_graph.ainvoke(initial_state)

        print("\n── PRICING DECISIONS ──────────────────────────────────────────")
        for rec in result["pricing_recommendations"]:
            change = rec["recommended_price"] - rec["current_price"]
            sign   = "+" if change >= 0 else ""
            print(
                f"  {rec['sku']:<12} {rec['action'].upper():<16} "
                f"PKR {rec['current_price']:>6.0f} → {rec['recommended_price']:>6.0f} "
                f"({sign}{change:.0f})  rung={rec['markdown_rung']}  "
                f"auto={rec['auto_executed']}  trigger={rec['trigger']}"
            )
            print(f"    {rec['reason']}")

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            print(f"  {alert['level'].upper()} [{alert.get('sku', '—')}]: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())