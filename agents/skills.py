"""
FashionOS Skills — Domain knowledge packages for each agent.

Each skill is a specialized prompt + context block that an agent loads
on-demand via load_skill(). This follows the LangChain Skills pattern:
progressive disclosure of domain knowledge without bloating the base
system prompt.
"""

SKILLS: dict[str, str] = {

    "fashion_inventory": """
## Fashion Inventory Intelligence Skill

You are now loaded with specialized knowledge for fashion inventory management.

### Fashion-specific inventory concepts
- **Dead stock threshold**: In fashion, any SKU with zero sales in 45+ days is considered
  dead stock. At 60+ days, aggressive markdown (30-40%) is typically required.
- **Velocity spike signal**: A SKU selling 3x its 7-day average in a 24-hour window
  indicates a trend break — treat as urgent.
- **Seasonal context**: Pakistani fashion demand peaks during Eid (Ramadan + Eid ul Fitr,
  then Eid ul Adha), summer (May-July), and winter (Nov-Jan). Interpret velocity
  numbers relative to the current season.
- **Size distribution pattern**: In Pakistani women's fashion, S/M typically outsell L/XL
  at roughly 40/35/15/10 ratio. Flag SKUs where L/XL are outselling S/M — it's a signal
  the sizing is running large.

### Stockout prediction formula
Days of stock remaining = current_inventory / units_per_day
- < 7 days: CRITICAL — restock order must go today
- 7-14 days: HIGH — restock order should go within 3 days
- 14-30 days: NORMAL — schedule restock
- > 30 days: HEALTHY — monitor only

### Supplier lead times (Pakistan context)
- Lahore/Faisalabad local manufacturers: 7-12 days
- Karachi textile traders: 5-10 days
- Alibaba/China imports: 18-30 days + customs (add 5-7 days buffer)
Always subtract lead time from days-of-stock when calculating urgency.
""",

    "fashion_trend": """
## Fashion Trend Intelligence Skill

You are now loaded with specialized knowledge for fashion trend analysis.

### Trend signal weighting
When multiple signals are available, weight them as follows:
1. TikTok video views (highest weight — fastest-moving signal)
2. Instagram Saves and Shares (strong purchase intent)
3. Google Trends search volume growth (confirms a trend is reaching mainstream)
4. Instagram Likes and Comments (engagement, but weaker purchase intent than saves)

A trend is STRONG when at least 2 of these signals align.
A trend is SPECULATIVE when only 1 signal is present.

### Fashion trend lifecycle (approximate)
- TikTok viral peak → Instagram mainstream: ~2-3 weeks
- Instagram mainstream → Google search peak: ~3-4 weeks
- Google search peak → retail saturation: ~4-6 weeks
Enter when TikTok signal is rising but Google has not yet peaked.

### Pakistani market context
- Modest fashion, co-ord sets, and lawn fabric dominate Pakistani Instagram
- Trending color cycles follow global TikTok trends with a ~3-week lag
- Dupes of viral international products (e.g. Zara sets, Mango cuts) sell extremely
  well in Pakistan at 40-60% of international price points
- TikTok Pakistan hashtags to monitor: #PakistaniFashion #PakistaniOutfits
  #FashionTikTokPK #OutfitOfTheDay #GRWM
""",

    "fashion_pricing": """
## Fashion Pricing Optimization Skill

You are now loaded with specialized knowledge for fashion pricing decisions.

### Pricing principles
- **Never discount trending items**: If Trend Agent signals a product is trending,
  hold or increase price by 5-10%. Scarcity + trend = price inelastic.
- **Markdown ladder**: Start with 15%, wait 10 days, then 25%, wait 10 days,
  then 35-40% final clearance. Never jump straight to 40%.
- **Bundle strategy**: Slow-moving items can be bundled with fast-moving ones
  at a combined price that protects margin better than individual markdowns.
- **Psychological pricing**: PKR 1999 outperforms PKR 2000. PKR 2499 outperforms
  PKR 2500. Always end in 99 or 499.

### Margin protection rules
- Never markdown below 35% gross margin (cost × 1.35) unless clearing dead stock
- "Dead stock" (60+ days, zero sales) — margin floor drops to cost + 10%
- High-trend items: maintain minimum 50% gross margin always

### Pakistani market price sensitivity
- Sweet spots: PKR 1500-2000 (entry), 2500-3500 (mid), 4000-6000 (premium)
- Price elasticity is HIGH below PKR 2000 — small discounts drive significant volume
- Price elasticity is LOW above PKR 4000 — quality/exclusivity positioning matters more
""",

    "fashion_content": """
## Fashion Content Creation Skill

You are now loaded with specialized knowledge for fashion content generation.

### Instagram caption formula (Pakistani fashion brands)
1. Open with a hook (trend reference, feeling, or bold claim) — 1 line
2. Product description woven naturally — 1-2 lines
3. Call to action (DM "WANT IT", link in bio, limited stock note) — 1 line
4. Hashtags (15-25, mix of niche and broad) — separate line

### TikTok script structure
- Hook (0-3s): Start with the end result — show the outfit first
- Problem or context (3-8s): "Looking for something to wear to..."
- Reveal (8-20s): Outfit details, styling tips, where to buy
- CTA (last 3s): "DM us for size guide / link in bio"

### Brand voice rules (applied across all content)
- Conversational Urdu-English mix acceptable ("yaar", "bilkul", "must-have")
- Never use generic fashion clichés ("stunning", "gorgeous", "look no further")
- Always mention at least one specific: fabric, cut, or occasion
- Urgency is real, not fake: "Limited stock" only if Inventory Agent confirms < 20 units

### Optimal posting times (Pakistan, PKT)
- Instagram: 8-9 PM daily (highest engagement window)
- TikTok: 7-9 PM daily
- Stories: 12-1 PM (lunchtime scroll) and 8-10 PM
""",

    "fashion_returns": """
## Fashion Returns Intelligence Skill

You are now loaded with specialized knowledge for fashion returns analysis.

### Return reason taxonomy
Cluster raw customer return reason text into ONE of these categories:

- **size_issue**: "too big", "too small", "sizing off", "runs large/small", "didn't fit",
  "size chart wrong"
  → Fix: Add precise cm/inch measurements to size guide. Add fit notes (e.g. "slim fit —
    size up if between sizes"). Photograph the garment flat with a ruler for scale.

- **description_mismatch**: "not as described", "color looks different in person",
  "fabric not what I expected", "different from photo", "color inaccurate on screen"
  → Fix: Reshoot product in natural outdoor light. Add color accuracy disclaimer.
    Add exact fabric composition + weight (grams per sqm) to description.

- **quality_issue**: "poor stitching", "bad quality fabric", "zipper broke on first wear",
  "color faded after one wash", "threading loose", "fell apart"
  → Fix: Flag supplier for quality review. Request a production batch hold.
    Consider removing the product until quality is resolved.

- **changed_mind**: "ordered by mistake", "didn't like how it looked on me",
  "found cheaper elsewhere", "gifted already have it", "no longer needed"
  → Fix: Monitor — high changed_mind may mean misleading marketing or impulse purchases.
    Consider adding more accurate lifestyle shots.

- **late_delivery**: "arrived too late for Eid", "wedding passed already",
  "needed for a specific event"
  → Fix: Add occasion-specific delivery warnings (e.g. "Order 7 days before your event").
    Show estimated delivery prominently on the product page.

- **duplicate_order**: "ordered twice by accident", "double charged"
  → Fix: No product fix needed. Review checkout UX or payment confirmation flow.

- **other**: anything that doesn't fit above categories clearly.

### Return rate thresholds (Pakistani fashion context)
Calculate: return_rate_pct = (total_units_returned / estimated_30d_sales) × 100
Where estimated_30d_sales = units_per_day × 30 (from Inventory Agent data if available).

- < 5%:  **healthy** — no action, just monitor
- 5-10%: **info** — log the reason, low priority fix
- 10-15%: **warning** — fix needed within 2 weeks
- > 15%: **critical** — immediate action, high revenue impact

If sales data is unavailable, use ABSOLUTE RETURN COUNTS as a proxy:
- < 3 units returned in 30 days: healthy
- 3-5 units: info
- 6-10 units: warning
- > 10 units: critical

### Pakistani fashion-specific return patterns
- Size guide issues are the #1 cause of returns in Pakistani fashion (60%+ of returns)
  — because most local brands don't provide cm measurements, only S/M/L labels
- Lawn and chiffon fabric returns spike in summer — customers expect specific drape/weight
  — fix: add fabric weight (grams per sqm) and drape description
- Color accuracy is a persistent problem — phone screens render fabric colors inaccurately
  — fix: outdoor natural light photos + color disclaimer in listing
- Returns for "changed mind" spike 2 weeks after Eid sales — impulse buys coming back
  — this is seasonal, not a product fix issue

### Fix priority by ROI
1. Size guide table with cm + inches → reduces size_issue returns ~40%
2. Natural light / accurate color photos → reduces description_mismatch ~30%
3. Fabric weight + feel description → reduces expectation mismatch ~20%
4. Occasion delivery warnings → reduces late_delivery returns ~60%
5. Supplier quality review → required for quality_issue patterns
""",

}


def load_skill(skill_name: str) -> str:
    """
    Load a specialized domain skill by name.
    Returns the skill prompt string, or an error message if not found.

    Available skills:
    - fashion_inventory  : Stock management, velocity, Pakistani supplier context
    - fashion_trend      : TikTok/IG trend signals, trend lifecycle, PK market context
    - fashion_pricing    : Markdown strategy, margin rules, Pakistani price sensitivity
    - fashion_content    : Caption/TikTok script formulas, brand voice, posting times
    - fashion_returns    : Return reason taxonomy, rate thresholds, PK-specific patterns
    """
    skill = SKILLS.get(skill_name)
    if skill is None:
        available = ", ".join(SKILLS.keys())
        return f"Skill '{skill_name}' not found. Available skills: {available}"
    return skill.strip()