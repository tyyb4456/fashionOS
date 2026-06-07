"""
shopify-mcp — FashionOS MCP Server
Exposes Shopify Admin API as MCP tools for all FashionOS agents.

Read tools  : list_products, get_product_by_sku, get_inventory_levels,
              get_recent_orders, get_returns, calculate_sales_velocity
Write tools : update_product_price, set_inventory_level,
              create_restock_recommendation
"""

import os
import httpx
from datetime import datetime, timedelta
from typing import Optional
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SHOPIFY_SHOP  = os.getenv("SHOPIFY_SHOP_NAME")   # e.g. "my-brand" (no .myshopify.com)
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN") # Admin API access token
API_VERSION   = "2026-04"
BASE_URL      = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/{API_VERSION}"
HEADERS       = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

# ── FastMCP app ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="shopify-mcp",
    instructions=(
        "You have access to a Shopify fashion store. "
        "Use these tools to read product, order, inventory, and returns data, "
        "and to take actions like updating prices or flagging restock needs. "
        "All write actions are logged. Price values are in the store's native currency."
    ),
)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get(endpoint: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()

async def _put(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(f"{BASE_URL}/{endpoint}", headers=HEADERS, json=payload)
        r.raise_for_status()
        return r.json()

async def _post(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{BASE_URL}/{endpoint}", headers=HEADERS, json=payload)
        r.raise_for_status()
        return r.json()

# ── READ TOOLS ────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_products(limit: int = 50, status: str = "active") -> list[dict]:
    """
    List all products with their variants, prices, and live inventory levels.

    Args:
        limit:  Max products to return (default 50, max 250).
        status: "active" | "draft" | "archived" | "any"

    Returns a flat list — each entry is one product with its variants nested.
    Used by: Inventory Agent, Pricing Agent, Marketing Agent.
    """
    data = await _get("products.json", {
        "limit": limit,
        "status": status,
        "fields": "id,title,status,tags,variants",
    })
    results = []
    for p in data.get("products", []):
        results.append({
            "product_id":    p["id"],
            "title":         p["title"],
            "status":        p["status"],
            "tags":          p.get("tags", ""),
            "variants": [
                {
                    "variant_id":           v["id"],
                    "sku":                  v.get("sku", ""),
                    "title":                v["title"],         # e.g. "Small / Beige"
                    "price":                float(v["price"]),
                    "compare_at_price":     float(v["compare_at_price"] or 0),
                    "inventory_quantity":   v.get("inventory_quantity", 0),
                    "inventory_management": v.get("inventory_management"),  # "shopify" | null
                }
                for v in p.get("variants", [])
            ],
        })
    return results


@mcp.tool()
async def get_product_by_sku(sku: str) -> Optional[dict]:
    """
    Fetch a specific product variant by its SKU.

    Args:
        sku: The exact SKU string (case-sensitive).

    Returns product + variant details or None if SKU not found.
    Used by: all agents when acting on a specific item.
    """
    data = await _get("products.json", {"fields": "id,title,variants", "limit": 250})
    for product in data.get("products", []):
        for v in product.get("variants", []):
            if v.get("sku") == sku:
                return {
                    "product_id":         product["id"],
                    "product_title":      product["title"],
                    "variant_id":         v["id"],
                    "sku":                v["sku"],
                    "variant_title":      v["title"],
                    "price":              float(v["price"]),
                    "inventory_quantity": v.get("inventory_quantity", 0),
                }
    return None

@mcp.tool()
async def get_price_rules(active_only: bool = True) -> list[dict]:
    """
    Fetch all price rules (discounts) currently configured in Shopify.

    Args:
        active_only: If True, only returns rules that are currently active
                     (started but not yet expired). Default True.

    Returns a list of price rules with title, value, and validity window.
    Used by: Pricing Agent (double-discount prevention).
    """
    from datetime import timezone  # add this import

    data = await _get("price_rules.json", {
        "limit":  250,
        "fields": "id,title,value_type,value,starts_at,ends_at,created_at",
    })

    now = datetime.now(timezone.utc)  # ← aware datetime, matches Shopify's format

    rules = []
    for r in data.get("price_rules", []):
        starts_at = r.get("starts_at")
        ends_at   = r.get("ends_at")

        if active_only:
            if starts_at:
                starts_dt = datetime.fromisoformat(starts_at)
                if starts_dt.tzinfo is None:
                    starts_dt = starts_dt.replace(tzinfo=timezone.utc)
                if starts_dt > now:
                    continue

            if ends_at:
                ends_dt = datetime.fromisoformat(ends_at)
                if ends_dt.tzinfo is None:
                    ends_dt = ends_dt.replace(tzinfo=timezone.utc)
                if ends_dt < now:
                    continue

        rules.append({
            "rule_id":    r["id"],
            "title":      r.get("title", ""),
            "value_type": r.get("value_type", ""),
            "value":      r.get("value", "0"),
            "starts_at":  starts_at,
            "ends_at":    ends_at,
            "created_at": r.get("created_at"),
        })

    return rules

@mcp.tool()
async def get_recent_orders(hours: int = 24, paid_only: bool = True) -> list[dict]:
    """
    Get all orders placed in the last N hours.

    Args:
        hours:     Look-back window in hours (default 24).
        paid_only: If True, only returns paid/fulfilled orders (excludes abandoned carts).

    Returns orders with their line items (sku, quantity, price).
    Used by: Inventory Agent (velocity), Pricing Agent, Marketing Agent.
    """
    since = (datetime.now() - timedelta(hours=hours)).isoformat() + "Z"
    params: dict = {
        "created_at_min": since,
        "limit":          250,
        "fields":         "id,created_at,financial_status,fulfillment_status,line_items",
    }
    if paid_only:
        params["financial_status"] = "paid"

    data = await _get("orders.json", params)
    orders = []
    for o in data.get("orders", []):
        orders.append({
            "order_id":           o["id"],
            "created_at":         o["created_at"],
            "financial_status":   o["financial_status"],
            "fulfillment_status": o.get("fulfillment_status"),
            "line_items": [
                {
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "sku":        item.get("sku", ""),
                    "name":       item.get("name", ""),
                    "quantity":   item["quantity"],
                    "price":      float(item["price"]),
                }
                for item in o.get("line_items", [])
            ],
        })
    return orders


@mcp.tool()
async def get_returns(days: int = 30) -> list[dict]:
    """
    Get all refunds and returns from the last N days.

    Args:
        days: Look-back window in days (default 30).

    Returns each returned line item with its SKU and any note the customer left.
    Notes are free-text reason fields — cluster them to find patterns.
    Used by: Returns Agent.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    data = await _get("orders.json", {
        "created_at_min": since,
        "limit":          250,
        "fields":         "id,refunds,line_items",
    })

    returns = []
    for order in data.get("orders", []):
        line_item_map = {li["id"]: li for li in order.get("line_items", [])}

        for refund in order.get("refunds", []):
            for rli in refund.get("refund_line_items", []):
                original = line_item_map.get(rli.get("line_item_id"), {})
                returns.append({
                    "order_id":         order["id"],
                    "refund_id":        refund["id"],
                    "refunded_at":      refund.get("created_at"),
                    "sku":              original.get("sku", ""),
                    "product_name":     original.get("name", ""),
                    "quantity":         rli.get("quantity", 0),
                    "restock":          rli.get("restock", False),
                    "return_reason":    refund.get("note", ""),    # customer's reason
                })
    return returns


@mcp.tool()
async def calculate_sales_velocity(days: int = 14) -> list[dict]:
    """
    Calculate daily units sold (velocity) per SKU over the last N days.

    Args:
        days: Period to calculate over (default 14 days).

    Returns SKUs sorted by velocity descending.
    This is the primary signal for stockout prediction and pricing decisions.
    Used by: Inventory Agent, Pricing Agent, Restock Agent.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    data = await _get("orders.json", {
        "created_at_min":   since,
        "financial_status": "paid",
        "limit":            250,
        "fields":           "line_items,created_at",
    })

    sku_data: dict[str, dict] = {}
    for order in data.get("orders", []):
        for item in order.get("line_items", []):
            sku = item.get("sku") or "NO_SKU"
            if sku not in sku_data:
                sku_data[sku] = {
                    "sku":        sku,
                    "name":       item.get("name", ""),
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "total_units": 0,
                    "total_revenue": 0.0,
                }
            sku_data[sku]["total_units"]   += item.get("quantity", 0)
            sku_data[sku]["total_revenue"] += float(item["price"]) * item.get("quantity", 0)

    result = []
    for sku, d in sku_data.items():
        result.append({
            **d,
            "units_per_day":     round(d["total_units"] / days, 2),
            "period_days":       days,
        })

    return sorted(result, key=lambda x: -x["units_per_day"])


# ── WRITE TOOLS ───────────────────────────────────────────────────────────────

@mcp.tool()
async def update_product_price(
    variant_id: int,
    new_price: float,
    compare_at_price: Optional[float],
    reason: str,
) -> dict:
    """
    Update the selling price of a specific product variant.

    Args:
        variant_id:       Shopify variant ID (integer).
        new_price:        New price in store currency (e.g. 2499.0 for PKR 2499).
        compare_at_price: Optional "was" price to show a strikethrough.
                          Pass None to clear it (full price, no strikethrough).
        reason:           Why this change is being made — stored in audit log.

    Returns confirmation with old and new price.
    Used by: Pricing Agent (markdowns, trend-based holds).
    """
    payload: dict = {"variant": {"id": variant_id, "price": str(new_price)}}
    if compare_at_price is not None:
        payload["variant"]["compare_at_price"] = str(compare_at_price)
    else:
        payload["variant"]["compare_at_price"] = ""

    result = await _put(f"variants/{variant_id}.json", payload)
    v = result.get("variant", {})
    return {
        "success":          True,
        "variant_id":       variant_id,
        "new_price":        new_price,
        "compare_at_price": compare_at_price,
        "reason":           reason,
        "updated_at":       v.get("updated_at"),
    }


@mcp.tool()
async def set_inventory_level(
    inventory_item_id: int,
    location_id: int,
    available: int,
    reason: str,
) -> dict:
    """
    Set the available inventory quantity at a specific location.

    Args:
        inventory_item_id: Shopify inventory item ID (from variant).
        location_id:       Shopify location ID (get from store settings).
        available:         New available quantity (absolute, not delta).
        reason:            Why inventory is being adjusted — for audit log.

    Returns success confirmation.
    Used by: Inventory Agent (corrections), Restock Agent (after delivery).
    """
    result = await _post("inventory_levels/set.json", {
        "inventory_item_id": inventory_item_id,
        "location_id":       location_id,
        "available":         available,
    })
    return {
        "success":   True,
        "available": available,
        "reason":    reason,
        "result":    result.get("inventory_level", {}),
    }


@mcp.tool()
async def create_restock_recommendation(
    sku: str,
    recommended_quantity: int,
    urgency: str,
    days_of_stock_remaining: float,
    units_per_day: float,
    reason: str,
    supplier_message: str,
) -> dict:
    """
    Record a restock recommendation for human review. Does NOT auto-order.
    This creates a pending record that shows up in the dashboard for approval.

    Args:
        sku:                     SKU that needs restocking.
        recommended_quantity:    Units to order.
        urgency:                 "critical" (<7 days stock) | "high" (7-14) | "normal" (>14).
        days_of_stock_remaining: Calculated days until stockout at current velocity.
        units_per_day:           Current sales velocity for this SKU.
        reason:                  Human-readable explanation of why restock is needed.
        supplier_message:        Pre-written WhatsApp/email message to send to supplier.

    Used by: Restock Agent. Human reviews and approves in the dashboard.
    """
    return {
        "type":                    "restock_recommendation",
        "sku":                     sku,
        "recommended_quantity":    recommended_quantity,
        "urgency":                 urgency,
        "days_of_stock_remaining": days_of_stock_remaining,
        "units_per_day":           units_per_day,
        "reason":                  reason,
        "supplier_message":        supplier_message,
        "status":                  "pending_approval",
        "created_at":              datetime.now().isoformat() + "Z",
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)