---
name: fashion-restock
description: >-
  Purchase order planning workflow for Pakistani Shopify fashion brands. Use when asked to:
  check what needs restocking, understand restock recommendations, get WhatsApp messages
  to send to suppliers, estimate the cost of pending orders, identify overdue purchase orders,
  understand why a SKU was or wasn't included in a restock recommendation, or run a fresh
  restock analysis. Covers quantity formulas, supplier selection logic, order deadlines vs
  stockout dates, Pakistani seasonal demand patterns, batch supplier messaging, and how
  restock decisions interact with pricing (clearance skip rule).
---

# Fashion Restock Skill

## Overview

The Restock Agent plans every purchase order — quantities, suppliers, deadlines, and
WhatsApp messages. The founder's only job is to review and approve. Once approved, they
copy-paste one message per supplier and the order goes out.

**Hard rule:** Every recommendation is `status="pending_approval"`.
FashionOS never auto-orders. Money leaving the business requires human sign-off.

---

## When to Run Restock Analysis

Trigger a restock analysis run when:
- Founder asks "what do I need to restock?", "what orders should go out?",
  "show me what to order this week", "give me the WhatsApp messages for suppliers"
- Daily pipeline run (always after inventory + pricing)
- Inventory agent has flagged critical or high urgency SKUs
- Founder wants to estimate this week's purchasing budget

**Execution order is mandatory:**
  1. `inventory-agent`  → inventory_snapshot (velocity, urgency, days_remaining)
  2. `pricing-agent`    → pricing_recommendations (clearance skip rule)
  3. `restock-agent`    → uses both, registers recommendations

Never run restock-agent before inventory-agent. The inventory_snapshot is required input.

---

## Step-by-Step Workflow

### Step 1 — Gather context

Call `get_inventory_status()` to confirm the latest snapshot exists.
Call `get_pending_approvals()` to see if restock recommendations already exist
from a previous run — no need to re-run the subagent if fresh data is available.

### Step 2 — Queue the restock agent (async)

start_agent_analysis(brand_id=<brand_id>, brand_name=<brand_name>, agents=["restock"])

Auto-includes inventory and pricing first (mandatory — restock needs the
clearance skip rule). No side effects — restock only ever writes pending_approval
rows, queue freely, no confirmation needed. Returns a task_id instantly;
acknowledge ("checking restock needs now, ~30-40s"), then
check_agent_analysis_status(task_id) on a later turn. Once "done", pull detail
via get_pending_approvals()["restock"].

### Step 3 — Interpret RestockAnalysis output

Check in this order:
1. `overdue_count > 0` → lead with these: order deadline has already passed
2. `critical_count > 0` → order deadline within days
3. `high_count > 0` → order within the week
4. `supplier_batches` → these are the actual messages to send

---

## Quantity Formula Reference

```
base_qty = ceil(units_per_day × (lead_time + 7)) - current_stock

seasonal_multiplier:
  Eid ul-Fitr run-up (2-3 weeks before):  1.5×
  Eid ul-Adha run-up (2-3 weeks before):  1.3×
  Pre-summer lawn season (Mar–Apr):       1.2×
  Wedding season (Oct–Feb):              1.15×
  Otherwise:                              1.0×

adjusted_qty = ceil(base_qty × seasonal_multiplier)

Floor: 20 units (MOQ)
Cap:   units_per_day × 60 (2-month max)
```

If `adjusted_qty ≤ 0` → stock already covers lead time + buffer. Skip.

---

## Order Deadline vs Stockout Date

These are different and both matter:

| Field | Formula | What it tells you |
|-------|---------|-------------------|
| `expected_stockout_date` | today + days_remaining | When you run out of stock |
| `order_deadline` | stockout_date − lead_days | When the PO must go OUT |
| `is_overdue` | order_deadline < today | You're already behind |

**Example:**
  days_remaining = 8, lead_days = 10
  stockout_date  = today + 8 = June 10
  order_deadline = June 10 − 10 = May 31
  If today is June 2 → is_overdue = True
  The stockout gap (June 10 → June 21) is unavoidable with standard lead time.

When presenting overdue orders to the founder, always mention:
- The stockout gap (how many days without stock)
- Expedited options: walk-in to Shadman Market (same-day), call supplier for emergency delivery

---

## Supplier Reference

| supplier_type    | Lead days | Best for | Typical MOQ |
|------------------|-----------|----------|-------------|
| lahore_local     | 10        | Lawn, kurtas, co-ords, suits, khaddar, shalwar — any Pakistani fabric item | 20–50 units |
| karachi_trader   | 7         | Plain cotton basics, essentials, unembellished cuts | 20–100 units |
| china_import     | 32 (incl. customs) | Accessories, bags, shoes, jewelry, novelty prints | 50–200 units |

**NEVER recommend china_import for a critical urgency SKU.**
32-day lead on a 5-day stockout = 27-day gap. Useless.

---

## Clearance Skip Rule

If Pricing Agent has assigned `action="clearance_code"` to a SKU, the Restock Agent
MUST skip it. Restocking a SKU that's being cleared is a contradiction — you'd be ordering
more of something you're trying to sell off cheaply.

This is enforced in the subagent, but worth verifying:
- If `get_pending_approvals()["pricing"]` shows `action="clearance_code"` for a SKU,
  do not recommend restocking it regardless of urgency classification.

---

## Supplier Batch Messages

The restock agent produces two levels of WhatsApp content:

**Individual SKU message** (`supplier_message` field per decision)
Used for single-SKU orders or when presenting to the founder SKU-by-SKU.

**Consolidated batch message** (`SupplierBatch.consolidated_message`)
One message per supplier covering all their SKUs in one WhatsApp.
This is what the founder actually sends — more natural for Pakistani supplier relationships
where everything happens in a single chat thread.

Present batches to the founder as:
```
📦 [SUPPLIER_TYPE] — [N] SKUs, [total_units] units (~PKR [cost])
Lead time: [lead_days] days | Deadline: [earliest order_deadline]

[consolidated_message — ready to copy-paste]
```

---

## Cost Estimate Reference

The restock agent uses category heuristics since Shopify doesn't store COGS.

| Product signals | Estimated landed cost PKR |
|-----------------|--------------------------|
| Lawn / cotton fabric | 900 |
| Khaddar / winter | 1,400 |
| Chiffon / formal / embroidered | 2,200 |
| Co-ord set / matching set | 1,800 |
| Cargo pants / bottoms | 900 |
| Accessories / bags / jewelry | 500 |

These are rough estimates. The founder should verify actual supplier price in the batch message reply before committing the order.

---

## Interpreting RestockAnalysis Output

```python
class RestockDecisionOut:
    sku, product_title, variant_title
    should_restock:          bool
    skip_reason:             Optional[str]   # why skipped
    recommended_quantity:    int
    urgency:                 str             # "critical" | "high"
    days_of_stock_remaining: float
    units_per_day:           float
    current_stock:           int
    supplier_type:           str
    estimated_lead_days:     int
    expected_stockout_date:  str             # when stock hits 0
    order_deadline:          str             # when PO must go OUT
    is_overdue:              bool            # already past order_deadline
    estimated_unit_cost_pkr:  Optional[float]
    estimated_total_cost_pkr: Optional[float]
    reason:                  str
    supplier_message:        str
    priority:                int             # 1 = most urgent
    status:                  str             # always "pending_approval"

class SupplierBatch:
    supplier_type:            str
    estimated_lead_days:      int
    skus:                     list[str]
    total_units:              int
    estimated_batch_cost_pkr: Optional[float]
    consolidated_message:     str            # copy-paste to WhatsApp

class RestockAnalysis:
    decisions:                list[RestockDecisionOut]
    supplier_batches:         list[SupplierBatch]
    total_units_to_order:     int
    estimated_total_spend_pkr: Optional[float]
    critical_count:           int
    high_count:               int
    overdue_count:            int
    skipped_count:            int
    summary:                  str
```

---

## Founder Briefing Format

```
 OVERDUE ORDERS (stockout gap unavoidable)
  [SKU] — [product / variant]
  Stock runs out: [expected_stockout_date] | Lead time: [lead_days]d
  Gap: [stockout_date] → [stockout_date + lead_days] (~[N] days without stock)
  Action: Walk-in to Shadman Market OR call supplier for emergency delivery

 CRITICAL ORDERS (order TODAY)
  [SKU] — [product / variant]
  [days_remaining] days left at [velocity]/day | Order [qty] units
  Order deadline: [order_deadline]

 HIGH PRIORITY ORDERS (order within 3 days)
  [SKU] — [product / variant] ...

 SUPPLIER MESSAGES READY TO SEND
  [lahore_local] — [N] SKUs, [total_units] units (~PKR [cost])
  [consolidated_message]
```

---

## Common Mistakes to Avoid

1. **Running restock before pricing** — the clearance skip rule requires pricing_recommendations.
   A SKU on clearance that also shows as "high urgency" will incorrectly get a restock order
   if pricing data isn't passed to the subagent.

2. **Confusing order_deadline with expected_stockout_date** — the founder needs to act by
   order_deadline, not stockout_date. Always present both and make the deadline prominent.

3. **Ignoring is_overdue=True** — these are the most urgent. A standard restock won't fix them.
   Expedited options (walk-in, same-day call) need to be surfaced explicitly.

4. **Approving a restock on a clearance-flagged SKU** — if the founder approved a clearance
   discount in the same session and now sees a restock recommendation, that's a data timing
   issue. Re-run the subagent with updated pricing_recommendations before approving the PO.

5. **Using china_import for any urgent SKU** — 32-day lead is only viable for healthy/normal
   urgency with > 45 days of stock remaining. Never for critical or high.
```