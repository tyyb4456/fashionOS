---
name: fashion-trend
description: >-
  Real-time fashion trend research workflow for the Pakistani market. Use when asked to:
  find what's trending on TikTok or Instagram, identify rising fashion keywords,
  match trends to catalog SKUs, discover new product opportunities (trending items
  not in the catalog), decide which products to push in content or ads this week,
  or interpret trend signals from the last pipeline run. Covers Pakistani hashtag
  strategy, signal quality evaluation, Google Trends cross-referencing, scoring
  methodology, and how to act on each signal type.
---

# Fashion Trend Skill

## Overview

This skill guides the complete trend research workflow for a Pakistani Shopify fashion brand.
Trend signals come from three sources — TikTok hashtags, Instagram hashtags, and Google Trends —
cross-referenced to produce scored, directional signals mapped to catalog SKUs.

**Two outcomes are possible per signal:**
- **Catalog match** — trend maps to a product already in stock → push content + ads
- **New product opportunity** — trend has no catalog match → flag for sourcing decision

---

## When to Run Trend Research

Trigger a trend research run when:
- Founder asks "what's trending?", "what should I post?", "what's hot right now?"
- Pipeline hasn't run in > 6 hours and a content or ad decision is pending
- An inventory SKU is sitting with zero velocity — trends data may explain why
- Restock decision is needed and the inventory snapshot alone isn't sufficient context
- Founder asks whether to source a new product type

Always call `get_inventory_status()` first to get the current catalog, then pass it to
the trend-agent task. The subagent cannot match SKUs without the catalog.

---

## Step-by-Step Workflow

### Step 1 — Build the catalog snapshot

Before calling trend-agent, extract a compact catalog from `get_inventory_status()`:

```python
catalog = [
    {"sku": s["sku"], "product_title": s["product"], "variant_title": s["variant"]}
    for s in inventory["skus"]
]
```

Pass this as JSON in the task message so the subagent can match SKUs.

---

### Step 2 — Queue the trend agent (async)

start_agent_analysis(brand_id=<brand_id>, brand_name=<brand_name>, agents=["trend"])

Returns a task_id almost instantly — runs agents/trend/graph.py's ReAct loop in
the background (auto-includes inventory first, since trend needs the catalog).
Tell the founder trend research has started (~15-30s), then check back with
check_agent_analysis_status(task_id) — once "done", result.run_summary and
get_open_alerts() / get_inventory_status() reflect the fresh signals.

The pipeline node autonomously:
1. Chooses Pakistani fashion hashtags appropriate for the catalog
2. Searches TikTok and Instagram, evaluating signal quality per result
3. Retries with different hashtags if results are thin (< 5 posts or near-zero engagement)
4. Cross-references strong social signals on Google Trends (Pakistan, geo=PK)
5. Scores and ranks all signals
6. Writes trend_signals + alerts to the database

---

### Step 3 — Interpret TrendAnalysis output

#### Signal scoring reference

| Score   | Meaning | Action |
|---------|---------|--------|
| 0.8–1.0 | Verified strong trend, multi-platform, rising | Prioritise immediately |
| 0.5–0.8 | Solid single-platform or moderate multi-platform | Act within 1–2 days |
| 0.3–0.5 | Weak or unconfirmed signal | Monitor, do not act yet |
| < 0.3   | Noise — excluded from output | No action |

#### Direction meanings

| Direction  | What it means | Implication |
|------------|---------------|-------------|
| "rising"   | Engagement growing, momentum building | Best time to act — early mover advantage |
| "peaking"  | At maximum, not accelerating | Act now before decline |
| "declining"| Falling from peak | Skip content push; let inventory clear organically |

---

### Step 4 — Act on each signal type

#### Catalog-matched signal (matched_sku is not null)

Cross-check matched SKU urgency from inventory:

| Inventory urgency | Trend score | Recommended action |
|-------------------|-------------|-------------------|
| healthy / normal  | ≥ 0.5       | Push content + increase ad budget |
| high / critical   | ≥ 0.5       | Push content immediately AND trigger restock |
| healthy / normal  | 0.3–0.5     | Queue content post, monitor next run |
| zero velocity     | ≥ 0.5       | Product may have discoverability issue — push content, review description |

**Never increase ad budget on a critical stockout SKU.** Driving traffic to
out-of-stock products wastes budget and damages brand trust.

#### New product opportunity (matched_sku is null, score ≥ 0.5)

Present to founder as a sourcing opportunity with:
- Trend keyword and platform evidence
- Score and direction
- Suggested product category to source
- Estimated lead time if local supplier (7–12 days Lahore/Faisalabad, 5–10 days Karachi)

Do NOT auto-source. Always require founder approval for new product decisions.

---

## Pakistani Market Hashtag Reference

These hashtags have historically strong engagement for Pakistani women's fashion.
The trend-agent will choose from these autonomously, but this reference helps interpret
which niches they map to:

| Hashtag | Niche |
|---------|-------|
| #PakistaniFashion | General women's fashion |
| #LawnSuit | Summer lawn fabric suits |
| #CoordSet | Matching co-ord sets |
| #EidOutfit | Festive / Eid wear |
| #KurtiDesign | Casual daily kurtas |
| #AbaayaStyle | Modest / abaya fashion |
| #ShalwarKameez | Traditional Pakistani suits |
| #ModestFashionPK | Modest wear broadly |
| #OOTDPakistan | Outfit of the day |
| #PakistaniWomenFashion | General women's category |

---

## Pakistani Seasonal Trend Patterns

Factor these into how you interpret scores and urgency:

| Period | What surges |
|--------|------------|
| Eid ul-Fitr (varies — check lunar calendar) | Formal lawn suits, festive co-ords, embroidered kurtas — 3–4 weeks before |
| Eid ul-Adha | Similar to Fitr but slightly less fashion-driven |
| Pre-summer (Mar–Apr) | Lawn season opens — lawn suit trends spike sharply |
| Wedding season (Oct–Feb) | Formal wear, heavy embroidery, chiffon, silk |
| Summer (May–Aug) | Light fabrics: cotton, lawn, linen, co-ords |
| Winter (Nov–Jan) | Khaddar, velvet, wool blends |

A score of 0.7 during Eid run-up is more urgent than the same score in an off-peak month.
Mention seasonality context when briefing the founder on trend actions.

---

## Google Trends Signal Reference

The trend-agent calls `compare_keywords()` and `get_related_queries()` to confirm social signals.
These values are relative (0–100 scale within the query period), NOT absolute search volumes.

| avg_interest | direction | Interpretation |
|---|---|---|
| > 60, rising | rising | Very strong confirmed trend |
| 30–60, rising | rising | Solid confirmation |
| 30–60, stable | stable | Background interest, not surging |
| < 30, any | any | Weak search volume — rely on social signal only |
| breakout (value ≥ 2000 in related_queries) | — | Explosive new term — high-confidence emerging signal |

---

## Output Schema Reference

```python
class TrendSignalOut:
    keyword:                    str      # e.g. "co-ord set", "cargo pants"
    platform:                   str      # "tiktok" | "instagram" | "google_trends"
    score:                      float    # 0.0–1.0
    direction:                  str      # "rising" | "peaking" | "declining"
    matched_sku:                Optional[str]  # SKU or None
    evidence:                   str      # platform, numbers, match rationale
    is_new_product_opportunity: bool     # True if score > 0.5 and no SKU match

class TrendAlertOut:
    level:   str           # "critical" | "info"
    message: str           # specific: keyword, platform, score, SKU or opportunity note
    sku:     Optional[str] # matched SKU or None

class TrendAnalysis:
    trend_signals: list[TrendSignalOut]  # score >= 0.3, sorted by score descending
    alerts:        list[TrendAlertOut]   # critical (matched rising) + info (opportunity)
    summary:       str                   # 2-3 sentences
```

---

## Common Mistakes to Avoid

1. **Running trend-agent without passing the catalog** — subagent cannot match SKUs and
   every signal comes back as a new product opportunity (false positives)

2. **Acting on a "peaking" signal like a "rising" signal** — peaking means the trend is
   at max, not growing; content push at peak still works but restock is too late

3. **Increasing ad budget on a trend-matched SKU that is critical/high urgency** —
   always cross-check trend output against inventory snapshot before any budget action

4. **Treating a declining signal as actionable** — declining trends should not trigger
   content or ad pushes; let the inventory clear at normal pace or apply a markdown

5. **Sourcing based on a single trend signal without Google Trends confirmation** —
   social signals can be one-off viral moments; Google Trends cross-reference filters noise

6. **Ignoring is_new_product_opportunity=True signals** — these are the highest-value
   output of trend research; a confirmed rising trend with no catalog match is a sourcing
   signal the founder needs to see

---

## Founder Briefing Format

When presenting trend results to the founder, structure it as:

```
🔥 TRENDING NOW
  [keyword] — [platform], score=[X], [direction]
  → [matched_sku product name] OR "not in catalog — sourcing opportunity"
  → Evidence: [1 sentence from evidence field]

📦 ACTION NEEDED
  [concrete recommendation: push content / increase budget / source / monitor]
```

Always include real numbers from the evidence field (views, likes, avg_interest).
Never present trend signals without the evidence — founders need to judge credibility.
```