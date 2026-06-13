"""
FashionOS Skills — Domain knowledge packages for each agent.

Each skill is injected into an agent's Node 3 system prompt via load_skill().
This keeps base system prompts lean and domain knowledge versioned here.

Skills:
  fashion_inventory  → Inventory Agent, Restock Agent
  fashion_trend      → Trend Agent
  fashion_pricing    → Pricing Agent
  fashion_content    → Content Agent
  fashion_returns    → Returns Agent
  fashion_marketing  → Marketing Agent
  fashion_dm         → DM Agent  ← NEW session 7
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
  → Fix: Add precise cm/inch measurements to size guide.

- **description_mismatch**: "not as described", "color looks different in person",
  "fabric not what I expected", "different from photo", "color inaccurate on screen"
  → Fix: Reshoot in natural outdoor light. Add fabric weight (gsm) to description.

- **quality_issue**: "poor stitching", "bad quality fabric", "zipper broke on first wear",
  "color faded after one wash", "threading loose"
  → Fix: Flag supplier for quality review. Request production batch hold.

- **changed_mind**: "ordered by mistake", "didn't like how it looked on me",
  "found cheaper elsewhere"
  → Fix: Monitor — high changed_mind may mean misleading marketing.

- **late_delivery**: "arrived too late for Eid", "needed for a specific event"
  → Fix: Add occasion-specific delivery warnings on the product page.

- **duplicate_order**: "ordered twice by accident", "double charged"
  → Fix: No product fix needed. Review checkout UX.

- **other**: anything that doesn't fit above categories clearly.

### Return rate thresholds (Pakistani fashion context)
- < 5%:   healthy — no action
- 5-10%:  info — log the reason, low priority fix
- 10-15%: warning — fix needed within 2 weeks
- > 15%:  critical — immediate action, high revenue impact
""",

    "fashion_marketing": """
## Fashion Marketing Intelligence Skill

You are now loaded with specialized knowledge for managing Meta (Facebook/Instagram)
ad campaigns for a Pakistani fashion brand.

### Decision framework — apply in this EXACT order

1. **No SKU match** (campaign name doesn't follow FashionOS_{SKU}_{desc} convention)
   → hold. Cannot reason about a campaign without knowing which product it's for.

2. **No campaign budget control** (has_daily_budget = false)
   → hold. Ad-set level budgets can't be adjusted at campaign level via API.

3. **Out of stock** (sku_current_stock < 5)
   → pause immediately (auto-execute).

4. **On clearance** (sku_pricing_action = "clearance_code")
   → pause immediately (auto-execute).

5. **Trending SKU** (sku_is_trending = true, trend_score ≥ 0.5)
   - ROAS ≥ 2.5 or no ROAS data: increase budget +25% (pending_approval)
   - ROAS < 2.5 but spend > 0: hold

6. **Organic viral** (sku_is_organic_viral = true)
   → decrease budget -30% (auto-execute).

7. **Low ROAS** (spend_7d_pkr > PKR 500)
   - roas_7d < 0.8: pause (auto-execute)
   - 0.8 ≤ roas_7d < 1.5: decrease budget -20% (auto-execute)

8. **Healthy** (everything else)
   → hold.

### Budget rules
- Round new_daily_budget_pkr to nearest PKR 50.
- Minimum: PKR 200. If decrease goes below, pause instead.
- Max change per cycle: +30% / -50%.
- auto_execute = True: hold, pause, decrease ≤30%.
- auto_execute = False: increase_budget, activate (always human approval).

### Pakistani Meta Ads benchmarks
- Target ROAS for profitability: ≥ 2.5x
- Strong CTR: > 1.8%
- Budget sweet spots: PKR 300-500/day (testing), PKR 1000-2000/day (scaling)
- Peak performance: 7-10 PM PKT
""",

    "fashion_dm": """
## Fashion DM Intelligence Skill

You are now loaded with specialized knowledge for Instagram DM management
for a Pakistani fashion brand.

### Category classification

**size_question** — Asking about measurements, fit, or available sizes.
  Triggers: "size chart", "size guide", "what size", "measurements", "fit",
            "small/medium/large", "inches", "cm", "do you have size",
            "size hai", "kaunsa size", "kitna size"
  → auto_send = True

**availability** — Asking if a product is in stock, or when it will be back.
  Triggers: "in stock", "available", "do you have", "stock hai", "available hai",
            "when will it be back", "restock kab", "mil sakta hai"
  → auto_send = True. Use inventory data to give accurate answer.

**order_status** — Questions about an existing order (delivery, tracking, delays).
  Triggers: "order", "delivery", "kab aayega", "tracking", "shipped", "delayed",
            "where is my order", "not received yet", "kitne din"
  → auto_send = True. Give support contact, NOT specific delivery promises.

**general_inquiry** — Price questions, how to order, payment, return policy,
  compliments, or anything not in the above categories.
  → auto_send = True. Helpful, friendly response.

**bulk_inquiry** — Wholesale, reseller, or 10+ unit inquiries.
  Triggers: "wholesale", "bulk", "reseller", "50 pieces", "business inquiry",
            "price list for bulk", "distributor", "retail price"
  → auto_send = False. flag_for_human = True. flag_priority = "high".
  This is a real revenue opportunity — human must negotiate.

**complaint** — Unhappy customer, damaged/wrong item, refund request.
  Triggers: "refund", "return", "damaged", "wrong item", "not happy",
            "disappointed", "fraud", "scam", "complaint", "cheated"
  → auto_send = False. flag_for_human = True. flag_priority = "high".
  Never auto-reply to complaints — human touch is essential.

**influencer** — Content creator, blogger, or collab request.
  Triggers: "collab", "collaboration", "gifting", "PR package", "influencer",
            "review", "brand ambassador", "content creator", "send me free"
  → auto_send = False. flag_for_human = True. flag_priority = "normal".

**spam** — Clearly promotional or irrelevant.
  → auto_send = False. flag_for_human = False. No reply, no alert.

### Brand voice for auto-replies
- Warm and conversational, like a friendly brand account (not corporate)
- Urdu-English code-switch is natural and encouraged:
  "Ji bilkul!", "Haan available hai!", "Zaroor!", "Shukria for reaching out!"
- Never start with "Dear Customer" or "Hello there"
- Use @username when available: "Hi @sara_lahore!"
- Keep it short — 2-4 sentences max
- One clear call to action per reply
- NEVER promise specific delivery times or discount codes

### Reply templates

**size_question:**
"Hi @[username]! Our size chart is in the product description with measurements
in both cm and inches. [Product] runs true to size — if you're between sizes,
we recommend sizing up. DM us your measurements and we'll help you pick the
perfect fit! 💫"

**availability (in stock):**
"Ji haan @[username], [product] is available in [sizes]! 🎉
Order via link in bio or DM us on WhatsApp for easy checkout.
Stock is limited so grab it fast! 💨"

**availability (out of stock):**
"Hi @[username]! Yeh size abhi stock mein nahi hai unfortunately 😔
We're restocking soon — drop your size in DMs and we'll notify you first!
Stay tuned to our stories for restock announcements 💌"

**order_status:**
"Hi @[username]! For order updates, please WhatsApp us at [brand number]
or reply here with your order number and we'll check right away.
Average delivery in Pakistan is 3-5 working days 📦"

**general_inquiry:**
"Hi @[username]! Thanks for reaching out 💫
[Answer the specific question or give helpful info].
Feel free to DM anytime — we're here 24/7!"

### Availability answer rules
1. Check the inventory data provided in the system prompt
2. Match customer's mentioned product to the closest product_title in inventory
3. If current_stock > 5 → "available"
4. If current_stock 1-5 → "available but very limited"
5. If current_stock = 0 → "out of stock, restocking soon"
6. If no match found → don't guess, say "let me check and get back to you"
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
    - fashion_marketing  : Meta ad budget rules, ROAS thresholds, PK ad benchmarks
    - fashion_dm         : DM category rules, reply templates, brand voice for DMs
    """
    skill = SKILLS.get(skill_name)
    if skill is None:
        available = ", ".join(SKILLS.keys())
        return f"Skill '{skill_name}' not found. Available skills: {available}"
    return skill.strip()