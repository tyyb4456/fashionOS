"""
FashionOS Deep Agent — Brand Memory Seeding
=============================================
/memories/AGENTS.md is the long-term memory file for one brand — persists
across every conversation for that brand_id (StoreBackend, namespaced by
brand_id). Seeded once, on first contact; the agent edits it directly
afterward via read_file/edit_file as it learns things about the brand.
"""

from langgraph.store.redis.aio import AsyncRedisStore


def seed_agents_md(brand_id: str, brand_name: str) -> str:
    return f"""# FashionOS Brand Memory — {brand_name}

## Brand Identity
- brand_id: {brand_id}
- brand_name: {brand_name}
- platform: Shopify + Meta Ads + Instagram
- currency: PKR (Pakistani Rupee)
- market: Pakistani fashion e-commerce

## Owner Preferences
<!-- Update when you learn something new about the brand owner.
Examples:
- prefers: bullet-point summaries only
- alert_channel: WhatsApp for critical alerts
- name: Tayyab -->

## Brand Rules
<!-- Overrides of global FashionOS defaults.
Examples:
- min_margin_floor: 38%
- price_endings: always PKR X99 or X499
- no_ad_budget_increase_on: Fridays -->

## Supplier Notes
<!-- Example: primary_supplier: Ahmed at Shadman Market, 5-day lead time -->

## Seasonal Patterns
<!-- Example: eid_velocity_multiplier: 3x normal (2 weeks before Eid) -->

## Past Decisions Log
<!-- Agent logs major decisions to avoid repeating bad ones -->
"""


async def ensure_brand_seeded(brand_id: str, brand_name: str, store: AsyncRedisStore) -> None:
    namespace = (brand_id,)
    key       = "/AGENTS.md"

    existing = await store.aget(namespace, key)
    if existing is None:
        from deepagents.backends.utils import create_file_data
        await store.aput(namespace, key, create_file_data(seed_agents_md(brand_id, brand_name)))
        print(f"[Memory] ✓ Seeded AGENTS.md for brand={brand_id}")
    else:
        print(f"[Memory] ✓ AGENTS.md already exists for brand={brand_id}, skipping seed")