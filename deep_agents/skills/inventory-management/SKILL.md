# Skill: Inventory Management

## When to Use This Skill
Load when asked about:
- Stock levels, stockout risk, days of stock remaining
- Which products need restocking
- Dead inventory (unsold 45+ days)
- Size/variant distribution for restock quantities

## Key Calculations
- Daily velocity = units sold last 14 days / 14
- Days remaining = current stock / daily velocity
- CRITICAL = <= 10 days | WARNING = <= 20 days | DEAD = 0 sales in 45 days

## Size Distribution Rule
Restock in same ratio as last 30 days sales.
Example: S:M:L:XL sold 10:35:30:15 → restock in same ratio.

## Output Format Per SKU
- Product + variant
- Current stock per size
- Daily velocity
- Days of stock remaining
- Status: CRITICAL / WARNING / HEALTHY / DEAD
- Recommended restock quantity (with size breakdown)