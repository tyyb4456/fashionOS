---
name: fashion-marketing
description: >-
  Autonomous Meta ad campaign management workflow for Pakistani Shopify fashion brands.
  Use when asked to: review ad campaign performance, understand why a campaign was paused
  or had its budget changed, check what budget changes are pending approval, interpret
  campaign ROAS data, decide whether to approve a pending budget increase, activate a
  paused campaign, understand how ad spend is allocated across trending vs slow products,
  or run a fresh marketing analysis. Covers the full decision framework, budget rules,
  SKU-campaign mapping convention, ROAS interpretation with and without Meta Pixel,
  auto-execute vs pending approval split, and how marketing interacts with inventory,
  trend, and pricing agents.
---

# Fashion Marketing Skill

## Overview

FashionOS marketing management keeps ad spend aligned with what's actually happening in
inventory and on TikTok — automatically, every cycle. No manual campaign monitoring needed.

**Core principle:** Ad spend follows the signal.
- Trending SKU in stock + good ROAS → increase budget (human approves)
- SKU out of stock → pause immediately (auto)
- SKU on clearance → pause (auto, ads on deeply discounted stock waste margin)
- ROAS below break-even with real spend → pause or cut budget (auto)
- Product selling organically at 2x store average → reduce ad burn (auto)
- Everything else → hold (no change)

**Two rails — same as pricing:**
- Autonomous rail: pause + budget decreases (safe, reversible, immediate)
- Approval rail: budget increases + campaign activation (real money, human reviews)

---

## When to Run Marketing Analysis

Trigger a marketing analysis run when:
- Founder asks "how are my ads doing?", "what campaigns should I pause?",
  "is my ad spend efficient?", "what's pending approval for ads?"
- Daily pipeline run — after inventory, trend, pricing agents have run
- A trending SKU alert fires and the founder asks about ad budget
- Founder approves a pending budget increase (confirm it was applied)
- A product goes out of stock and founder wants to confirm its campaign was paused

**Execution order is mandatory:**
  1. `inventory-agent`  → inventory_snapshot (stock, velocity, urgency)
  2. `trend-agent`      → trend_signals (scored, matched to SKUs)
  3. `pricing-agent`    → pricing_recommendations (clearance flags)
  4. `marketing-agent`  → uses all three for cross-referenced decisions

Never run marketing-agent in isolation without inventory context — it needs stock levels
and pricing flags to make correct pause/hold decisions. Running it blind means it can't
catch the clearance contradiction (never restock or run ads on something being cleared out).

---

## Step-by-Step Workflow

### Step 1 — Check if fresh data exists

Call get_pending_approvals() first. If marketing is non-empty and the last run
was under 6 hours ago, present existing data rather than queuing a new run.

### Step 2 — Confirm before queuing (MANDATORY)

Marketing can auto-execute real Meta changes the moment the run lands — pausing
out-of-stock/clearance campaigns, decreasing budget on low ROAS or organic viral
SKUs. Tell the founder plainly what could auto-apply and get an explicit yes
before proceeding to Step 3.

### Step 3 — Queue the marketing agent (async)

start_agent_analysis(brand_id=<brand_id>, brand_name=<brand_name>, agents=["marketing"])

Auto-includes inventory, trend, pricing first (mandatory — marketing needs stock
levels, trend scores, and clearance flags). Returns a task_id instantly.
Acknowledge the ad review has started (~40-60s for the 4-agent chain), then
check_agent_analysis_status(task_id) on a later turn. Once "done", report from
result.marketing (total_decisions, auto_executed, pending_approval) and
get_pending_approvals()["marketing"] for detail.


### Step 4 — Interpret MarketingAnalysis output

Check in this order:
1. `failed_count > 0` → Meta API errors during execution — check ads-mcp :8004
2. `paused_count > 0` → report which campaigns were auto-paused and why
3. `pending_count > 0` → present for founder approval (budget increases, activations)
4. Summary → present overall state

---

## Decision Framework Reference

Ten rules applied in strict priority order. First matching rule wins.

| Priority | Rule | Trigger | Auto? |
|----------|------|---------|-------|
| 1 | No SKU match in campaign name | `no_sku_match` | ✓ hold |
| 2 | No campaign-level budget control | `no_budget_control` | ✓ hold |
| 3 | Matched SKU out of stock (< 5 units) | `out_of_stock` | ✓ pause |
| 4 | Matched SKU on clearance | `clearance` | ✓ pause |
| 5 | Trending SKU, score ≥ 0.5 | `trending_*` | ✗ increase (pending) |
| 6 | PAUSED campaign + trending SKU | `paused_trending` | ✗ activate (pending) |
| 7 | Organic viral (2x+ store avg velocity) | `organic_viral` | ✓ decrease -30% |
| 8 | ROAS < 0.8, spend > PKR 500 | `very_low_roas` | ✓ pause |
| 9 | ROAS 0.8–1.5, spend > PKR 500 | `low_roas` | ✓ decrease -20% |
| 10 | Everything else | `healthy` | ✓ hold |

---

## Budget Rules

All budget changes follow these hard constraints:

- **Rounding:** Always to nearest PKR 50 (487→500, 512→500, 625→650)
- **Minimum:** PKR 200. If decrease would go below → use pause instead
- **Max increase per cycle:** +30% (meta learning phase protection)
- **Max decrease per cycle:** −50%
- **Auto-execute ceiling:** |change_pct| ≤ 30 to auto-execute a decrease

---

## Auto-Execute vs Pending Approval

```
auto_execute = True
  → "hold"                             (no write needed)
  → "pause"                            (Rules 3, 4, 7 at floor, 8)
  → "decrease_budget" |change_pct| ≤ 30 (Rules 7, 9)

auto_execute = False (always)
  → "increase_budget"                  (any amount)
  → "activate"                         (any condition)
```

For pending approvals, present to the founder as:
```
PENDING APPROVAL — Budget Change
  Campaign: {campaign_name}
  SKU: {matched_sku} — {product_title}
  Action: {action} {change_pct:+.0f}%
  Budget: PKR {current_daily_budget_pkr:.0f} → PKR {new_daily_budget_pkr:.0f}
  ROAS (7d): {roas_7d or "No Pixel"}
  Trend: {trend_score} ({trend_direction})
  Reason: {reason}
  [Approve] [Reject]

PENDING APPROVAL — Campaign Activation
  Campaign: {campaign_name} (currently PAUSED)
  SKU: {matched_sku} — trending {trend_score:.2f} ({trend_direction})
  Reason: {reason}
  [Activate] [Keep Paused]
```

---

## SKU-Campaign Mapping Convention

The marketing agent extracts SKUs from campaign names automatically.
Convention: `FashionOS_{SKU}_{short_desc}`
Example:    `FashionOS_FOS-001-S_OliveCargo`

Campaigns not following this convention get `matched_sku=None` and are always held.
The supervisor should flag non-compliant campaign names to the founder:
"3 campaigns couldn't be mapped to inventory because they don't follow the
FashionOS_{SKU}_{desc} naming convention. Rename them to enable auto-optimisation."

---

## ROAS Interpretation

**With Meta Pixel installed on Shopify store:**
```
ROAS < 0.8                → very_low_roas → pause
ROAS 0.8–1.5              → low_roas → decrease -20%
ROAS 1.5–2.5              → break-even to profitable → hold or increase if trending
ROAS ≥ 2.5                → strong → increase if trending (pending)
```

**Without Meta Pixel (roas_7d = None):**
```
Rely on CTR as proxy engagement signal.
CTR > 2% + trending SKU → increase (pending), noting pixel is absent
CTR < 2% or no_spend     → hold (not enough signal to justify increase)
```

**Installing Meta Pixel** dramatically improves decision quality. If ROAS is consistently
null, remind the founder to install the Shopify Meta Pixel app and configure Purchase events.

---

## Marketing × Inventory × Trend × Pricing Interactions

| Inventory urgency | Trend signal | Pricing action | Marketing decision |
|---|---|---|---|
| critical (< 7d) | any | any | hold/pause campaign (stock won't last) |
| out of stock (< 5 units) | any | any | **pause** (Rule 3, auto) |
| healthy | rising, score ≥ 0.5 | hold | increase if ROAS ≥ 1.5 (pending) |
| healthy | rising, score ≥ 0.5 | clearance_code | **pause** (Rule 4 overrides Rule 5) |
| any | rising | clearance_code | **pause** (clearance always wins over trend) |
| any | no signal | ROAS < 0.8 | **pause** (Rule 8, auto) |
| any | any | any | CTR + ROAS determines hold/decrease |

Key interaction: **clearance beats everything**. If pricing-agent has a SKU on clearance,
the marketing-agent always pauses its campaign regardless of trend score or ROAS.

---

## Pakistani Ad Market Context

Eid and seasonal budget justification (mention when presenting pending increases):

| Period | Budget premium |
|--------|---------------|
| Eid ul-Fitr run-up (2-3 weeks before) | +50–100% justified |
| Pre-summer lawn season (Mar–Apr) | +30–50% justified |
| Winter arrivals (Oct–Nov) | +20–30% justified |
| Wedding season (Oct–Feb) | +20–40% justified |

During peak seasons, approve budget increases more readily — demand is genuinely higher
and the ROAS justification needs a lower bar than off-peak periods.

---

## Output Schema Reference

```python
class CampaignDecisionOut:
    campaign_id, campaign_name
    matched_sku:              Optional[str]   # SKU from campaign name
    current_daily_budget_pkr: float
    current_status:           str             # "ACTIVE" | "PAUSED"
    has_daily_budget:         bool
    roas_7d:                  Optional[float] # None if no Pixel
    spend_7d_pkr:             float
    ctr_7d:                   float
    no_spend_data:            bool
    action:                   str             # hold/pause/activate/increase/decrease
    new_daily_budget_pkr:     Optional[float]
    change_pct:               float
    trigger:                  str
    auto_execute:             bool
    reason:                   str
    executed:                 bool            # True = Meta API confirmed
    execution_result:         Optional[str]   # "success" | error string

class MarketingAnalysis:
    decisions:           list[CampaignDecisionOut]  # ALL campaigns, no omissions
    auto_executed_count: int
    pending_count:       int
    failed_count:        int
    paused_count:        int
    summary:             str
```

---

## Founder Briefing Format

```
 AUTO-PAUSED (out-of-stock / clearance / very low ROAS)
  [campaign_name] — SKU {sku}
  Reason: {reason}

 AUTO-DECREASED (organic viral / low ROAS)
  [campaign_name] — PKR {current:.0f} → PKR {new:.0f} ({change_pct:.0f}%)
  Reason: {reason}

 PENDING APPROVAL — Budget Increases
  [campaign_name] — PKR {current:.0f} → PKR {new:.0f} (+{change_pct:.0f}%)
  ROAS: {roas_7d or 'No Pixel'} | Trend: {score:.2f} ({direction})
  [Approve] [Reject]

 PENDING APPROVAL — Campaign Activation
  [campaign_name] — paused, {sku} trending {score:.2f}
  [Activate] [Keep Paused]

 HELD ({n} campaigns — healthy / no change needed)
```

---

## Common Mistakes to Avoid

1. **Running marketing-agent before pricing-agent** — without clearance flags, it may
   recommend a budget increase on a SKU that's actively being cleared at -35% discount.
   The clearance-beats-trend rule only works if pricing_recommendations are passed.

2. **Approving a budget increase on a critical-urgency SKU** — even if ROAS is excellent,
   increasing ad spend on a SKU with < 7 days of stock drives traffic to something that
   will be out of stock before the ads optimise. Check inventory urgency before approving.

3. **Ignoring failed_count** — if any Meta API call failed, the campaign is out of sync
   (analytics say it should be paused, but it's still running). Always check failed_count
   and re-run or manually act if > 0.

4. **Treating ROAS=None as ROAS=0** — they mean different things.
   None = no pixel, no data. 0 = pixel installed, zero conversions tracked. Different rules apply.

5. **Approving unlimited budget increases during Eid without checking stock** — the seasonal
   premium justifies higher spend only if inventory can fulfil the demand. Cross-check
   days_remaining before approving any increase near peak seasons.

6. **Not fixing non-compliant campaign names** — `no_sku_match` campaigns are always held
   and can never be auto-optimised. Renaming them to FashionOS_{SKU}_{desc} unlocks full
   autonomy. Surface this to the founder on every run where non-compliant campaigns exist.
```