---
name: fashion-dm
description: >-
  Instagram DM management workflow for Pakistani Shopify fashion brands. Use when asked to:
  check Instagram DMs, see what customers are asking, find out if there are any complaints,
  review bulk orders that came in via DM, check influencer collaboration requests, understand
  why a DM was flagged, see patterns in what customers are asking about, get action items
  from DM analysis, run a fresh DM sweep, or interpret results from the last DM agent run.
  Covers category taxonomy, auto-send rules, reply drafting, Pakistani brand voice, availability
  cross-referencing, bulk inquiry revenue estimation, pattern analysis, and how DM signals
  connect to inventory, content, and returns decisions.
---

# Fashion DM Skill

## Overview

FashionOS DM management answers customer messages autonomously, 24/7. A customer sends a
DM at 2am — they get a reply in under 60 seconds instead of waiting until morning and
buying from a competitor.

**Two rails:**
- **Auto-send rail:** safe, factual categories answered immediately (size, availability,
  order status, general questions, price inquiries)
- **Draft rail:** revenue-sensitive or risk-sensitive categories get a drafted reply
  waiting for founder approval (complaints, bulk orders, influencer collabs)

---

## When to Run DM Analysis

Trigger the dm-agent subagent when:
- Founder asks "check our DMs", "any complaints?", "any bulk orders?", "what are customers asking?"
- Scheduled every 30 minutes via Celery beat (independent of daily pipeline)
- After a product goes viral — high DM volume expected
- Founder wants to know if there are influencer collabs pending
- Returns agent has flagged a product — check if complaints are coming in via DM too

**Schedule is independent.** DMs don't wait for the daily pipeline. The dm-agent runs
on its own 30-minute cycle AND can be called conversationally by the founder anytime.

**Optional enrichment:** If inventory-agent has run recently, pass inventory_snapshot so
availability answers use live stock data. If returns-agent has run, pass return_insights
so return-related replies are product-specific, not just policy generic.

---

## Step-by-Step Workflow

### Step 1 — Check existing flagged DMs

Call `get_open_alerts(brand_id, level="warning")` to see if the previous DM run already
flagged items that are still unresolved. Present those to the founder before triggering
a new run if they haven't been addressed.

### Step 2 — Gather optional enrichment context

For richer replies:
```python
# Get latest inventory (for availability answers)
inventory_data = await get_inventory_status(brand_id)
inventory_snapshot = inventory_data.get("skus", [])

# Get return insights (for return policy answers)
return_insights = await get_return_insights(brand_id)
if isinstance(return_insights, list) and "error" not in str(return_insights[0]):
    return_insights_clean = return_insights
else:
    return_insights_clean = []
```

### Step 3 — Confirm before queuing, then queue (async)

DM can auto-send real Instagram replies to real customers the moment the run
lands (size/availability/order-status/general/pricing categories). Tell the
founder plainly that customer replies may go out automatically and get an
explicit yes before queuing — UNLESS this is the routine 30-minute scheduled
sweep, which already runs on its own via Celery beat and never touches chat.

start_agent_analysis(brand_id=<brand_id>, brand_name=<brand_name>, agents=["dm"])

No dependencies — queues immediately. Returns a task_id instantly. Acknowledge
DM processing has started (~10-20s), then check_agent_analysis_status(task_id)
on a later turn. Once "done", report from result.dm (auto_replied, flagged) and
get_open_alerts() / batch pattern insights for what was flagged.
```

### Step 4 — Interpret DmAnalysis output

Check in this order:
1. `critical_flags` → surface immediately, these need founder response NOW
2. `batch_stats.action_items` → systemic fixes (size guide, photography, etc.)
3. `decisions` where `flag_for_human=True` → present with draft replies for approval
4. `send_results` → confirm what was sent, flag any failures
5. `summary` → overall DM health

---

## Category Taxonomy

| Category | Sub-categories | Auto-send | Priority if flagged |
|---|---|---|---|
| `size_question` | fit_advice, measurement_request, size_comparison, size_for_event | ✓ | — |
| `availability` | *(none)* | ✓ | — |
| `order_status` | *(none)* | ✓ | — |
| `pricing_inquiry` | discount_request, cod_availability, bundle_price, wholesale_rate | ✓ (except discount promises) | — |
| `general_inquiry` | *(none)* | ✓ | — |
| `bulk_inquiry` | reseller, wedding_party, corporate, gifting, boutique | ✗ | high / critical if > PKR 30k |
| `complaint` | delivery_delay, quality_issue, wrong_item, return_request, color_mismatch | ✗ | critical if frustrated |
| `influencer` | nano_under10k, micro_10k_100k, macro_100k_plus, unknown_tier | ✗ | normal |
| `spam` | *(none)* | ✗ | — (no flag, no reply) |

---

## Auto-Send Rules

```
auto_send = True ONLY for:
  size_question        ← factual, no risk, customer expects quick answer
  availability         ← factual, uses live inventory data, time-sensitive
  order_status         ← requests order number, directs to track link
  general_inquiry      ← return policy, shipping info, brand questions
  pricing_inquiry      ← price info (NEVER commit to discount)

auto_send = False ALWAYS for:
  bulk_inquiry         ← real money involved, human must confirm pricing
  complaint            ← churn risk, human touch required
  influencer           ← partnership decision needs founder judgement
  spam                 ← no action taken
```

**Key rule:** Even with `auto_send=True`, if `reply_confidence="low"` (ambiguous message),
the subagent notes this in send_results. The reply was sent but founder can spot-check.

---

## Revenue Intelligence (Bulk Inquiries)

The DM agent estimates PKR order value for every bulk_inquiry:

```
estimated_order_value_pkr = quantity_mentioned × avg_product_price

Where avg_product_price = 2,500 PKR (default if unknown)
```

Flag priority by value:
| Estimated order value | flag_priority |
|---|---|
| > PKR 30,000 | `critical` (appears in critical_flags) |
| PKR 10,000–30,000 | `high` |
| < PKR 10,000 | `normal` |

Present bulk inquiries to the founder with the draft reply + revenue estimate:
```
💰 BULK INQUIRY — @{username}
Category: bulk_inquiry / {sub_category}
Estimated order: ~PKR {value:,} ({quantity} units)
Priority: {flag_priority}

Draft reply (review and send manually):
"{reply_text}"

Recommended action: {follow_up_action}
```

---

## Complaint Routing

Complaints are NEVER auto-sent. They always get a human-reviewed draft.

Sentiment determines flag_priority:
| sentiment | sub_category | flag_priority |
|---|---|---|
| frustrated | any | `critical` |
| neutral/urgent | wrong_item, quality_issue | `high` |
| neutral | color_mismatch, return_request | `high` |
| neutral | delivery_delay | `high` |

For `wrong_item` or `quality_issue`: the draft reply includes an apology + replacement offer.
The founder should send this within 2 hours to prevent negative reviews.

For `color_mismatch`: cross-check if returns-agent has flagged this product.
If it has, the reply is more empathetic and includes a fix acknowledgment.

---

## Influencer Evaluation

When an influencer reaches out, the subagent:
1. Estimates tier from follower count mentioned (nano / micro / macro)
2. Sets flag_priority=normal (all collab decisions are human-driven)
3. Drafts a warm reply asking for email + engagement stats

Founder decides whether to pursue. The supervisor should note:
- Macro influencers (>100k): escalate_to_founder
- Micro (10k-100k): pursue if niche matches brand
- Nano (<10k): gifting only if budget allows

---

## Pattern Analysis — What to Look For

The batch_stats.pattern_insights field flags:

| Pattern | Threshold | Implication |
|---|---|---|
| Multiple size questions, same product | ≥ 2 in batch | Size guide needs cm measurements |
| Multiple color mismatch complaints | ≥ 2 in batch | Photography review needed |
| Multiple availability DMs, same product | ≥ 3 in batch | High demand signal — check stock |
| Multiple quality complaints | ≥ 2 in batch | Check latest supplier batch |
| Bulk inquiry > PKR 25k | Any single | Founder personal response recommended |

These patterns also generate `action_items` — concrete tasks for the founder:
```
action_items:
  - "Update size guide for Olive Cargo Pants with chest/waist/length in cm (4 size questions)"
  - "Review beige dress photography — shoot in natural daylight only (2 color mismatch)"
  - "Respond personally to @retailer_pk before they go to a competitor (PKR 75,000 bulk order)"
```

Action items from DM analysis are high-ROI — one size guide update can eliminate 10 support
messages per day permanently.

---

## Pakistani Brand Voice Reference

Auto-sent replies follow these rules (embedded in subagent, listed here for review):

✓ Urdu-English code-switching — minimum 2 Urdu phrases
✓ Address @username directly by name
✓ Warm, human tone — like texting a helpful friend
✓ ONE CTA per reply
✓ Max 500 chars (Instagram DM limit is 1000, shorter = better)

✗ Never "Dear Customer"
✗ Never promise specific delivery dates
✗ Never promise ad-hoc discounts
✗ Never state stock count if > 20 (creates unnecessary panic)
✗ Never multiple CTAs

**Urdu phrases that work well:**
`yaar`, `bilkul`, `zaroor`, `shukriya`, `kal khatam ho jayega`, `sirf X pieces bache hain`,
`bohot acha choice hai`, `DM karein`, `koi sawaal ho toh puchein`, `JazakAllah`

---

## Availability Reply Logic

The subagent cross-references inventory_snapshot to give accurate availability answers:

```
current_stock > 10  → "Haan, available hai — link in bio se order karein!"
1–10 units          → "Sirf {n} pieces bache hain! Jaldi karein — link in bio."
0 / critical        → "Filhal stock out hai — size DM karein, restock pe pehle notify karein ge!"
Product not found   → "Confirm karein ge — kaunsa size/color chahiye?" (reply_confidence=medium)
```

If inventory_snapshot was not passed to the subagent, all availability answers default to
the "product not found" response (safe, never misleads). Always pass inventory data for
accurate replies.

---

## DM Agent Outputs — Schema Reference

```python
class DmDecisionOut:
    message_id, conversation_id, user_id, username
    original_message: str          # truncated to 300 chars
    category: str                  # one of 9 categories
    sub_category: Optional[str]    # refined classification
    sentiment: str                 # positive/neutral/frustrated/urgent/excited
    auto_send: bool
    reply_text: Optional[str]      # ready-to-send or draft for human
    flag_for_human: bool
    flag_priority: Optional[str]   # critical/high/normal/None
    flag_reason: Optional[str]     # why human attention needed
    products_mentioned: list[str]  # for pattern analysis
    estimated_order_value_pkr: Optional[float]  # bulk_inquiry only
    follow_up_action: Optional[str]  # concrete next step
    reply_confidence: str          # high/medium/low

class DmBatchSummary:
    total_fetched, total_processed
    auto_sent_count, flagged_count, skipped_spam_count, low_confidence_count
    category_breakdown: dict
    top_products_mentioned: list[str]
    pattern_insights: list[str]
    action_items: list[str]

class DmAnalysis:
    decisions:     list[DmDecisionOut]
    batch_stats:   DmBatchSummary
    critical_flags: list[str]     # conversation_ids needing immediate attention
    send_results:  list[dict]     # execution results for auto-sent replies
    summary:       str
```

---

## Founder Briefing Format

```
 DM SUMMARY — {brand_name} (@{instagram_handle})
Run: {timestamp} | {total_processed} DMs processed

 AUTO-REPLIED ({auto_sent_count})
  {n} size questions, {n} availability, {n} order status, {n} general

⚠  NEEDS YOUR ATTENTION ({flagged_count})
  [CRITICAL] @{username} — {category}/{sub_category}
    "{original_message[:80]}..."
    Estimated value: PKR {value:,}
    Draft reply: "{reply_text}"
    Action: {follow_up_action}
    [Send Reply] [Edit First]

  [HIGH] @{username} — {category}
    "{original_message[:80]}..."
    Reason: {flag_reason}
    Draft reply: "{reply_text}"

 ACTION ITEMS FROM PATTERN ANALYSIS
  • {action_item_1}
  • {action_item_2}

 SEND FAILURES (if any)
  @{username} — reply failed: {error}. Retry or send manually.
```

---

## Common Mistakes to Avoid

1. **Not passing inventory_snapshot** — availability replies become generic ("we'll check")
   instead of accurate ("3 pieces left!"). Always grab from DB first with `get_inventory_status()`.

2. **Treating complaint drafts as ready-to-send** — complaints need human personalisation.
   The draft is a starting point, not a final message. Always present as "[DRAFT — review before sending]".

3. **Ignoring pattern_insights** — the highest-value output of a DM run is often not the
   individual replies but the patterns. "5 size questions = update size guide" eliminates
   those questions permanently at the source.

4. **Not escalating high-value bulk inquiries immediately** — a PKR 75,000 reseller order
   sitting in the DM queue for 6 hours often goes to a competitor. critical_flags exist for
   a reason — surface them to the founder the moment a run completes.

5. **Sending pricing_inquiry replies that mention any discount** — even "we sometimes have
   sales" trains customers to wait. Reply states current price only, directs to link in bio.

6. **Forgetting follow_up_action on complaint decisions** — 'create_return_label',
   'review_product_photos', 'update_size_guide' are the operational outputs that reduce
   future complaints. Don't lose them in the flag details.

7. **Running DM agent only in daily pipeline** — DMs are time-sensitive (customers won't
   wait 24 hours). The 30-minute Celery schedule is mandatory for competitive response times.
```