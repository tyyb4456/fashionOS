---
name: inventory-management
description: >-
  Full inventory analysis workflow for Shopify fashion brands. Use when asked to:
  check stock levels, identify stockout risk, calculate daily sales velocity per SKU,
  flag dead inventory (unsold 14+ days), detect size distribution anomalies, or run a
  complete inventory health check. Returns a structured analysis with urgency-ranked
  snapshots and actionable alerts. Covers Pakistani supplier lead times and local
  size distribution patterns.
---

# Inventory Management Skill

## Overview
This skill guides a complete inventory health analysis for a Shopify fashion brand.
It fetches live data via MCP tools, computes velocity and days-of-stock, classifies
each SKU by urgency, and produces structured output ready for the dashboard.

---

## Step-by-Step Workflow

### Step 1 — Fetch Shopify Data

Call both tools in parallel (or sequentially if parallel is unavailable):

```
list_products(brand_id=<brand_id>, limit=250, status="active")
calculate_sales_velocity(brand_id=<brand_id>, days=14)
```

**Critical filter:** Skip any variant where `inventory_management != "shopify"`.
These variants have `inventory_quantity = None` or a meaningless default — including them
creates false stockout alerts. Only track what Shopify actually manages.

**Skip variants with no SKU assigned.** Log a warning and continue; don't fail.

---

### Step 2 — Build the Velocity Lookup

Create a dict of `sku → units_per_day` from `calculate_sales_velocity` output:
```python
velocity_map = {row["sku"]: row["units_per_day"] for row in velocity_data if row.get("sku")}
```

---

### Step 3 — Compute Per-Variant Metrics

For each tracked variant (shopify-managed, SKU assigned):

| Field | Formula |
|---|---|
| `units_per_day` | `velocity_map.get(sku, 0.0)` |
| `days_of_stock_remaining` | `current_stock / units_per_day` if `units_per_day > 0` else `999.0` |
| `zero_velocity` | `True` if `units_per_day == 0 AND current_stock > 0` |

Round `days_of_stock_remaining` to 1 decimal place.

---

### Step 4 — Classify Urgency

Apply **exactly** these thresholds:

| `days_of_stock_remaining` | `urgency` |
|---|---|
| `< 7` | `"critical"` |
| `7 ≤ x < 14` | `"high"` |
| `14 ≤ x ≤ 30` | `"normal"` |
| `> 30` | `"healthy"` |
| `= 999` (zero velocity) | `"normal"` — NOT critical (not selling out, just not selling) |

**Important:** A `zero_velocity` SKU is dead stock (potential warning), not a stockout risk.
Never set urgency = "critical" for a SKU with `units_per_day = 0`.

---

### Step 5 — Generate Alerts

Raise alerts only when they require human attention or action.

**CRITICAL alerts:**
- `days_of_stock_remaining < 7` → "SKU {sku} ({product_title} / {variant_title}): {current_stock} units left, ~{days} days at current velocity ({units_per_day:.2f}/day). RESTOCK TODAY."

**WARNING alerts (dead stock):**
- `current_stock > 0` AND `units_per_day == 0` (zero velocity in 14-day window):
  "SKU {sku}: {current_stock} units with zero sales in 14 days. Consider 15% markdown or bundle."

**INFO alerts (size anomaly):**
- For a product with multiple size variants (S/M/L/XL), if combined L+XL velocity > combined S+M velocity:
  "Product {product_title}: L/XL variants outselling S/M ({lxl:.2f}/day vs {sm:.2f}/day). Sizing likely runs large — update size guide."

**Do NOT raise alerts for:**
- Healthy SKUs with stock > 30 days
- SKUs with `units_per_day > 0` and `days_remaining > 14`
- Variants that were filtered out (no SKU, not shopify-managed)

---

### Step 6 — Pakistani Size Distribution Check

Expected S:M:L:XL velocity ratio ≈ 40:35:15:10.

If a product shows L/XL outselling S/M consistently:
- Raise an INFO alert about sizing
- Include both velocity numbers so the founder can decide whether to update the size guide

---

### Step 7 — Build the Summary

Write a 2–3 sentence summary:
- Lead with critical count and most urgent SKU
- Include warning count and dead stock count  
- Close with overall health percentage

Example:
> "2 SKUs critical (restock today): FOS-042-S (3 days) and FOS-017-M (6 days).
> 4 dead stock variants flagged for markdown review. 18 SKUs healthy."

---

## Output Schema Reference

The structured output (`InventoryAnalysis`) has:

```python
class SnapshotOut:
    sku: str
    product_title: str
    variant_title: str
    current_stock: int           # ≥ 0
    units_per_day: float         # ≥ 0.0
    days_of_stock_remaining: float   # 999.0 for zero-velocity
    urgency: str                 # "critical" | "high" | "normal" | "healthy"

class AlertOut:
    level: str                   # "critical" | "warning" | "info"
    message: str                 # specific: SKU, numbers, action needed
    sku: Optional[str]           # the SKU this alert is about (if applicable)

class InventoryAnalysis:
    inventory_snapshots: list[SnapshotOut]   # ALL tracked variants, one per row
    alerts: list[AlertOut]                   # only actionable alerts
    summary: str                             # 2-3 sentences
```

**Include ALL tracked variants in `inventory_snapshots`** — not just problematic ones.
The dashboard needs the full picture to render the inventory table.

---

## Common Mistakes to Avoid

1. **Including untracked variants** — always check `inventory_management == "shopify"` first
2. **Setting zero-velocity SKUs to critical** — 999 days remaining is NOT a stockout
3. **Aggregating variants** — each size/colour is its own row; don't merge S+M into one entry
4. **Missing the lead time context** — always mention lead time in restock alerts so the
   founder knows whether "7 days of stock" is actually an emergency given their supplier
5. **Omitting the variant title** — "Olive Cargo Pants" is useless; "Olive Cargo Pants / Small"
   is actionable

---

## Pakistani Supplier Lead Time Reference

| Source | Lead Time |
|---|---|
| Lahore / Faisalabad local manufacturers | 7–12 days |
| Karachi textile traders | 5–10 days |
| China / Alibaba | 18–30 days + 5–7 days customs buffer |

When writing CRITICAL alerts, include:
```
Stock runs out in ~{days} days. Supplier delivers in {lead_time} days → ORDER IMMEDIATELY.
```