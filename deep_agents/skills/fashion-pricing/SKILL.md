---
name: fashion-pricing
description: >-
  Autonomous pricing workflow for Pakistani Shopify fashion brands. Use when asked to:
  review pricing decisions, understand why a price changed, check what's pending approval,
  interpret markdown ladder state for any SKU, decide whether to approve a pending pricing
  action, understand why a clearance code was created, or run a fresh pricing sweep.
  Covers the full markdown ladder, Pakistani psychological pricing rules, auto-execute
  thresholds, trend-aware price increases, and how pricing interacts with inventory urgency.
---

# Fashion Pricing Skill

## Overview

FashionOS pricing operates on two rails simultaneously:
- **Autonomous rail** — actions within safe thresholds are executed immediately, zero latency
- **Approval rail** — actions above risk thresholds appear in the dashboard for founder review

The goal is maximum margin preservation with zero manual work on routine decisions.
The founder's time is spent only on large discounts, bundle strategy, and aggressive increases.

---

## When to Run Pricing

Check get_pending_approvals() / get_inventory_status() first — if a recent run
already covers the question, answer from that. Queue a fresh run only when:
- Founder asks about current prices / pending approvals AND the last run is stale
- Inventory has flagged dead stock (zero velocity) needing markdown decisions
- Trend has rising signals — trending SKUs may need a price increase
- Founder approved a pending action in the dashboard (confirm it applied)

**⚠ Confirm before queuing.** Pricing can auto-execute real Shopify price changes
(first-rung markdowns, trending increases, clearance codes within threshold — see
Auto-Execute Rules below). Tell the founder plainly what could auto-apply and get
an explicit yes before calling start_agent_analysis with "pricing".

start_agent_analysis(brand_id=<brand_id>, brand_name=<brand_name>, agents=["pricing"])

Auto-includes inventory and trend first (mandatory order — pricing needs
velocity/urgency and trend signals as context). Returns a task_id instantly.
Acknowledge that pricing review has started (~20-40s for the 3-agent chain), then
check_agent_analysis_status(task_id) on a later turn. Once "done", report from
result.pricing (total_decisions, auto_executed, pending_approval) and
get_pending_approvals()["pricing"] for line-item detail.

---

## The Markdown Ladder

Every SKU tracks its discount history via Shopify's `compare_at_price` field.
No separate database needed — the strikethrough price IS the state.

| Rung | compare_at_price | Discount | Auto-execute? | Trigger |
|------|-----------------|----------|---------------|---------|
| 0    | 0 (not set)     | 0%       | —             | Full price, never discounted |
| 1    | = original price | ~15%    | ✓ YES         | First zero-velocity detection |
| 2    | = original price | ~25%    | ✓ YES         | Confirmed dead stock (persists) |
| 3    | = original price | ≥35%    | ✓ YES         | Stage-3 clearance code |
| —    | = original price | >35%    | ✗ NO          | Requires approval (deep cut) |

**Key rule:** `compare_at_price` is NEVER reset between rungs. Once set to the original
price, it stays there forever so the customer always sees the full original "was" price.

---

## Auto-Execute Rules (what runs without approval)

| Action | Conditions | Rationale |
|--------|-----------|-----------|
| `hold` (any) | Always | No write — nothing to approve |
| `markdown` 15% | Rung 0, zero velocity | First markdown — fully reversible, low risk |
| `markdown` 25% | Rung 1, zero velocity confirmed | Dead stock — second rung needed |
| `increase` ≤10% | Trending SKU, stock > 20, velocity > 1.5/day | Data-backed, easily reversed |
| `clearance_code` | Rung 2, stock > 10 | Deep dead stock — auto-code created |

---

## Pending Approval Rules (what goes to the dashboard)

| Action | Why it needs approval |
|--------|----------------------|
| `markdown` > 25% | Large margin impact — founder eyes needed |
| `increase` > 10% | Higher brand risk — founder verifies market conditions |
| `increase` (non-trending) | No data signal — increase without trend basis needs review |
| `clearance_code`, stock ≤ 10 | Too small quantity — not worth the discount code friction |
| `bundle` | Requires manual Shopify bundle product creation |

---

## Trend × Pricing Interaction

When trend_signals are available, pricing decisions change:

| Trend signal | Inventory urgency | Pricing action |
|---|---|---|
| Rising, matched SKU | healthy/normal | Hold or increase 5-10% |
| Rising, matched SKU | high/critical | Hold (don't discount a selling-out trending item) |
| Rising, matched SKU | zero velocity | Override dead-stock logic — hold, investigate content first |
| Peaking, matched SKU | healthy/normal | Hold — trend may sustain |
| Declining, matched SKU | zero velocity | Proceed with markdown ladder normally |
| No match (new product opportunity) | — | No pricing action (different SKU) |

**Never mark down a trending SKU.** Even zero-velocity trending SKUs may just have
discoverability issues — push content before cutting price.

---

## Pakistani Psychological Pricing

All recommended prices end in 99 or 499. This is non-negotiable.

| Target | Round to |
|--------|---------|
| PKR 2540 | 2499 |
| PKR 2560 | 2599 |
| PKR 3000 | 2999 |
| PKR 4000 | 3999 or 4499 (pick nearest) |
| PKR 1200 | 1199 |
| PKR 800  | 799 |

Never end in round 00, 50, or any digit other than 9.

---

## Interpreting PricingAnalysis Output

When the pricing analysis returns its structured output, check these fields:

```
decision.executed = True   → price has already been changed in Shopify this run
decision.executed = False + auto_execute = True  → execution failed (check execution_result)
decision.auto_execute = False + action != "hold" → in the approval queue
```

For pending approvals, present to the founder as:
```
PENDING APPROVAL
  [SKU] — [product_title] / [variant_title]
  Action: [action] | [discount_pct]% → PKR [current_price] → PKR [recommended_price]
  Reason: [reason field]
  [Approve] [Reject]
```

For executed actions, the daily digest includes them as completed items.
The founder does NOT need to review already-executed actions unless they disagree.

---

## Founder Override Logic

If the founder rejects a pending pricing action in the dashboard:
1. Do NOT re-recommend the same action on the next run (respect the rejection)
2. Note the rejection in AGENTS.md under ## Brand Rules:
   `no_markdown_{sku}: rejected on {date} — hold at full price`
3. The pricing agent reads AGENTS.md brand rules and will skip that SKU on future runs

If the founder manually changes a price in Shopify outside FashionOS:
- The next run will pick it up via `list_products` and recalculate rung correctly
- compare_at_price is the source of truth for ladder state

---

## Double-Discount Prevention

The pricing agent checks existing Shopify price rules before creating clearance codes.
But the supervisor should also check:
- If `get_pending_approvals()` already shows a pending clearance for the same SKU,
  do not trigger the pricing-agent again for that SKU until it's resolved
- Manual Shopify discounts not created by FashionOS will be detected via get_price_rules

---

## Common Mistakes to Avoid

1. **Running pricing-agent before inventory-agent** — velocity data exists in pricing's
   own fetch, but urgency and days_remaining from inventory snapshot are richer.
   Always run inventory first.

2. **Approving a price increase on a critical-urgency SKU** — increases on nearly-OOS
   items are fine, but only if restock is already ordered. Check restock status first.

3. **Treating executed=False as pending approval** — failed executions (API errors)
   appear as `executed=False` with `auto_execute=True`. These are errors, not approvals.
   Check `execution_result` field to differentiate.

4. **Ignoring bundle recommendations** — when a SKU hits rung 3 with no velocity,
   bundle is the last resort before writing off the inventory. Present it to the founder
   with a specific bundle partner SKU suggestion (another slow-mover in the same category).

5. **Resetting compare_at_price on second/third markdown** — this breaks the ladder.
   The customer should always see "Was PKR 3999, Now PKR 2599", not "Was PKR 3399, Now PKR 2599".
   The was-price is always the original full price.

---

## Margin Floor Reference (Pakistani Fashion)

Never let recommended_price imply a margin below these floors.
If you don't have COGS data, use these category-level heuristics:

| Category | Typical landed cost | Minimum sell price |
|---|---|---|
| Lawn / cotton suits | PKR 800–1200 | PKR 1799 |
| Khaddar / winter wear | PKR 1200–2000 | PKR 2799 |
| Chiffon / formal | PKR 1800–3000 | PKR 3999 |
| Co-ord sets | PKR 1500–2500 | PKR 3499 |
| Cargo pants / bottoms | PKR 700–1200 | PKR 1699 |

If a clearance recommendation would breach these floors, cap it at the minimum and
flag the SKU for a write-off review instead.

---

## Output Schema Reference

```python
class PricingDecisionOut:
    sku, variant_id, product_title, variant_title
    current_price:        float
    compare_at_price:     float    # 0 if not on markdown
    recommended_price:    float    # always ends in 99 or 499
    new_compare_at_price: Optional[float]
    action:               str      # "hold" | "markdown" | "increase" | "clearance_code" | "bundle"
    discount_pct:         float
    markdown_rung:        int      # rung AFTER this action (0–3)
    auto_execute:         bool     # whether this was within auto-execute thresholds
    executed:             bool     # whether update_product_price actually ran
    execution_result:     Optional[str]  # "success" | error string
    suggested_discount_code: Optional[str]
    reason:               str

class PricingAnalysis:
    decisions:           list[PricingDecisionOut]
    auto_executed_count: int
    pending_count:       int
    failed_count:        int
    summary:             str
```
