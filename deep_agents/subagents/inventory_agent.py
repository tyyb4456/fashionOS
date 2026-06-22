from response_schemas.inventory_model import InventoryAnalysis

INVENTORY_AGENT_PROMPT = """
You are FashionOS Inventory Agent — a specialist in Shopify inventory analysis.

Your job:
1. Fetch all products
2. For each product, get inventory levels and sales data
3. Calculate per variant:
   - daily_velocity = units_sold_last_14d / 14
   - days_remaining = current_stock / daily_velocity (→ 999 if no sales)
4. Classify each SKU:
   - CRITICAL: days_remaining <= 10
   - WARNING:  days_remaining <= 20
   - HEALTHY:  days_remaining > 20 with sales
   - DEAD:     zero sales in last 45 days
5. Restock qty = 30 days stock at current velocity, split by size ratio of last 30d sales
6. Return a structured InventoryAnalysis

Always include numbers. No vague language.
"""


async def build_inventory_subagent(tools: list) -> dict:
    """
    Returns the inventory subagent dict.
    tools: MCP tools passed in from supervisor after await client.get_tools()
    """
    return {
        "name": "inventory-agent",
        "description": (
            "Analyzes all Shopify inventory. Calculates velocity, days of stock remaining, "
            "identifies CRITICAL/WARNING/DEAD SKUs, and recommends restock quantities with "
            "size breakdowns. Call this when you need a full inventory health check."
        ),
        "system_prompt": INVENTORY_AGENT_PROMPT,
        "tools": tools,
        "response_format": InventoryAnalysis,
    }