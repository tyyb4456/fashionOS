---
name: fashion-content
description: >-
  Instagram and TikTok content planning workflow for Pakistani fashion brands.
  Use when asked to: generate content for today's posts, find out what to film this week,
  get Instagram captions or TikTok scripts, understand why a SKU wasn't included in the
  content plan, check what's urgent to post today, get creator shot lists, align organic
  content with an active Meta campaign, or run a fresh content generation pass.
  Covers 5-tier candidate selection, content angle taxonomy, return-issue creator overrides,
  campaign-content sync, Pakistani brand voice rules, seasonal framing, and shot list format.
---

# Fashion Content Skill

## Overview

FashionOS content planning generates ready-to-publish Instagram captions, TikTok scripts,
Instagram story hooks, and specific shot lists — aligned with what's actually happening
in inventory, trends, pricing, and paid ads.

**Core principle:** Content follows the signal.
- Trending SKU in stock → post TODAY, riding the trend wave
- On markdown → schedule this week, drive clearance velocity
- Active Meta campaign → sync organic content with ad intent (organic boosts ad relevance score)
- Clearance stock with quantity → one urgency post before it's gone
- OOS → never generate content (wastes brand credibility)
- Trending + clearance simultaneously → skip (contradictory signals damage trust)

**Two tracks:**
- Urgent today: trending + clearance with low stock → post within hours
- Scheduled this week: markdown + campaign-sync + lifestyle → schedule via Buffer/Later

---

## When to Run Content Planning

Trigger the content-agent subagent when:
- Founder asks "what should I post today?", "what's ready to film?", "generate captions"
- Daily pipeline has run and content is the next step
- A trend alert fired and the founder wants content to match
- A markdown was just applied to a SKU and promotion is needed
- A Meta campaign is pending budget approval and organic content should amplify it
- Founder asks why a specific SKU is not in the content queue

**Execution order is mandatory:**
  1. `inventory-agent`  → inventory_snapshot (stock, velocity, urgency)
  2. `trend-agent`      → trend_signals (scored signals, matched SKUs)
  3. `pricing-agent`    → pricing_recommendations (markdown/clearance flags)
  4. `marketing-agent`  → marketing_actions (active campaigns, pending increases)
  5. `returns-agent`    → return_insights (optional, but improves creator notes)
  6. `content-agent`    → ContentPlan (uses all five above)

Content agent is the only subagent that benefits from ALL prior agents having run.
Never run it before inventory and trend at minimum.

---

## Step-by-Step Workflow

### Step 1 — Check existing content queue

Call `get_content_queue()` to see if pending posts already exist from a recent run.
If urgent posts exist (is_urgent=True) that haven't been published, present those first
before triggering a new run.

### Step 2 — Collect context from prior agents

Gather from prior subagent results in this session:
- `inventory_snapshot` from inventory-agent
- `trend_signals` from trend-agent
- `pricing_recommendations` from pricing-agent
- `marketing_actions` from marketing-agent (use [] if unavailable)
- `return_insights` from returns-agent result (use [] if unavailable)
- `current_date` in YYYY-MM-DD format

If prior results aren't available: call `get_inventory_status()` as fallback for inventory,
`get_pending_approvals()["pricing"]` for pricing context. Trend signals may be absent —
content agent will fall back to markdown + campaign-sync candidates only.

### Step 3 — Delegate to content-agent

```
task(
    name="content-agent",
    task=(
        "Generate content plan for {brand_name} (brand_id={brand_id}). "
        "current_date: {YYYY-MM-DD} "
        "inventory_snapshot: {inventory_json} "
        "trend_signals: {trend_signals_json} "
        "pricing_recommendations: {pricing_json} "
        "marketing_actions: {marketing_json} "
        "return_insights: {return_insights_json} "
        "Select candidates, generate Instagram + TikTok content, return ContentPlan."
    )
)
```

### Step 4 — Interpret ContentPlan output

Check in this order:
1. `priority_today_skus` → these need to be posted within hours
2. `fatigue_skips` → if founder asks why a SKU isn't in the plan, check here first
3. `posts` sorted by urgency → present urgent posts first with full caption + shot list
4. `summary` → use as the quick brief to the founder

---

## Candidate Selection Algorithm

Five tiers applied in strict order. First matching tier wins. Total cap: 5 posts.

| Tier | Condition | Max | is_urgent | Angle | Post when |
|------|-----------|-----|-----------|-------|-----------|
| 1 | Trending (score≥0.4, rising/peaking) + in stock + NOT clearance | 3 | True | trending | today |
| 2 | On markdown + in stock + NOT trending + NOT clearance | 2 | False | markdown_push | within-3-days |
| 3 | Active Meta campaign (pending increase) + stock>10 + NOT clearance | 1 | False | ad_content_sync | tomorrow |
| 4 | Clearance + stock>20 + NOT trending | 1 | stock<30 | clearance_push | today/this-week |
| Skip | OOS (stock≤5) | — | — | — | never |
| Skip | Trending + clearance simultaneously | — | — | — | never |
| Skip | Critical urgency + stock<10 + not trending | — | — | — | never |

---

## Content Angles

| Angle | Hook style | Urgency | Special rules |
|-------|-----------|---------|---------------|
| `trending` | Reference trend implicitly — don't say "trending" | High | Never say "This is trending". Show it via context. |
| `markdown_push` | Price front and center | Normal | State old price and new price explicitly |
| `clearance_push` | Scarcity first, then price | High if stock<30 | "Last [N] pieces" — only if stock<20 |
| `ad_content_sync` | Value prop lead, hook like a 3-sec ad | Normal | Aligns with Meta ad creative intent |
| `lifestyle` | Aesthetic and seasonal, no urgency | Low | No price/discount mention |

---

## Return Issue Creator Note Overrides

When a SKU has known return issues, the creator notes include MANDATORY filming instructions.
These are non-optional — suppressing them risks repeating the return pattern.

| Return reason | Mandatory creator note |
|---------------|----------------------|
| `size_issue` | Film garment next to ruler showing chest/waist/length. Say measurements out loud in TikTok reveal. Add cm measurements to caption. |
| `color_mismatch` | Natural daylight ONLY. No ring lights, no color presets, no filters. Caption note: "Color shown in natural light." |
| `quality_issue` | Close-up of fabric texture and stitching. Mention fabric composition out loud. Minimal editing. |
| `description_mismatch` | Read product specs out loud in TikTok reveal. Caption must match exactly what's filmed. |

---

## Shot List Format

Every content plan has 3-5 specific shots per SKU:

| Shot # | Type | Platform | Always required |
|--------|------|----------|----------------|
| 1 | `flat_lay` | both | Always — clean background, natural light, no accessories |
| 2 | `mirror_try_on` | tiktok primary | Always — full outfit, this is the TikTok hook frame |
| 3 | `detail_closeup` | instagram | Always — fabric texture, print, hardware, embroidery |
| 4 | `lifestyle` | both | Always — natural, candid, seasonal background |
| 5 | conditional | varies | `pricing_card` (sale) / `measurement` (return issue) / `transition` (trending) |

---

## Pakistani Brand Voice Rules

MANDATORY across all generated content:

✓ Urdu-English code-switching — minimum 2-3 Urdu words per caption
✓ Specific product facts: fabric name, cut, occasion, size, price
✓ ONE CTA per content piece — never list multiple options
✓ "Limited stock" ONLY when current_stock < 20

✗ NEVER: stunning, gorgeous, must-have, look no further, elevate your look
✗ NEVER: Start hook with brand name or product name
✗ NEVER: Multiple CTAs in same piece

---

## Seasonal Content Framing

| Month | Frame as | Creator notes direction |
|-------|----------|------------------------|
| March–April | Lawn season opening, summer-ready | Outdoor light, bright setting |
| May–August | Summer staple, lightweight, breathable | Natural outdoor, light tones |
| October–February | Wedding season, event-ready, formal | Indoor elegant, warm ambient light |
| Any | Eid/festive (if product tags include eid/formal/embroidered) | Rich indoor, jewel tones |

---

## Output Schema Reference

```python
class InstagramOut:
    caption:           str           # 80-150 words, hook→body→CTA
    hashtags:          list[str]     # 20-25, no # symbol
    story_hook:        Optional[str] # separate story format, more urgent
    optimal_post_time: str           # "20:00 PKT"

class TikTokOut:
    hook:              str  # 0-3s: outcome first
    context:           str  # 3-8s: relatable setup
    reveal:            str  # 8-20s: product details + price
    cta:               str  # last 3s: one action
    optimal_post_time: str  # "19:00 PKT"

class ShotListItem:
    shot_number:  int  # 1-indexed, lower = higher priority
    description:  str  # specific, actionable, no vague adjectives
    platform:     str  # "instagram" | "tiktok" | "both"
    shot_type:    str  # flat_lay / mirror_try_on / detail_closeup / lifestyle / etc.

class ContentPostOut:
    sku, product_title, variant_title
    is_urgent:              bool
    urgency_reason:         str    # with data — "Trending score=0.87..."
    content_angle:          str    # trending / markdown_push / etc.
    post_date_suggestion:   str    # "today" | "tomorrow" | "within-3-days" | "this-week"
    instagram:              InstagramOut
    tiktok:                 TikTokOut
    creator_notes:          str    # overall direction + mandatory return notes if applicable
    shot_list:              list[ShotListItem]
    is_trending:            bool
    trend_keyword:          Optional[str]
    trend_score:            Optional[float]
    is_on_sale:             bool
    discount_pct:           float
    sale_mention:           Optional[str]
    has_return_issue:       bool
    return_issue_type:      Optional[str]
    has_active_campaign:    bool
    current_stock:          int
    status:                 str  # "pending"

class ContentFatigueSkip:
    sku, product_title
    skip_reason: str  # oos / trend_clearance_contradiction / critical_stock_no_restock / etc.

class ContentPlan:
    posts:                list[ContentPostOut]
    fatigue_skips:        list[ContentFatigueSkip]
    priority_today_skus:  list[str]
    total_posts:          int
    urgent_count:         int
    summary:              str
```

---

## Founder Briefing Format

```
 POST TODAY (urgent — trending / clearance closing)
  [SKU] — [product / variant]
  Angle: [content_angle]
  Why today: [urgency_reason]

   Instagram (20:00 PKT):
  [first 100 chars of caption]...
  Hashtags: #[top 5 hashtags]... (+[N] more)

   TikTok (19:00 PKT):
  Hook: [tiktok.hook]
  CTA: [tiktok.cta]

   Shot list:
  1. [shot #1 description]
  2. [shot #2 description]
  ...

   Creator notes: [creator_notes]

 SCHEDULED THIS WEEK
  [SKU] — [product / variant] | [post_date_suggestion]
  Angle: [angle] | [discount_pct]% off (if on sale)
  ...

⏭ SKIPPED
  [SKU] — [skip_reason]
```

---

## Common Mistakes to Avoid

1. **Running content-agent before trend-agent** — without trend signals, Tier 1 candidates
   are always empty. The whole plan degrades to markdown/clearance only, missing the
   highest-impact posts.

2. **Approving content for an OOS SKU** — the content agent hard-skips these, but if the
   founder manually requests content for a specific SKU that's in fatigue_skips with "oos",
   explain why it was excluded and ask them to confirm stock levels first.

3. **Ignoring fatigue_skips** — when the founder asks "why isn't [product] in the plan?",
   the answer is always in fatigue_skips. Check there before re-running the subagent.

4. **Not checking return_insights** — if return_insights are available from returns-agent,
   always pass them. The mandatory creator note overrides for size/color/quality issues
   directly reduce repeat returns — high ROI from one extra field.

5. **Treating post_date_suggestion as flexible** — "today" means within the optimal posting
   window (19:00-20:00 PKT). If the founder sees this in the afternoon, the window is the
   SAME DAY. Remind them of the timing context.

6. **Running content before marketing-agent** — without marketing_actions, campaign-sync
   Tier 3 candidates are always empty, missing content-ad alignment opportunities.

7. **Content for a trending + clearance SKU** — the subagent detects this contradiction and
   skips it. If the founder asks to promote a clearance SKU that's also trending, explain:
   "Running content on a clearance-priced trending item confuses customers — they'll share
   the post then find it's discounted, which signals low quality. Let the clearance run
   without content promotion."
```

