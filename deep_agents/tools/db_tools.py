"""
FashionOS Deep Agent — DB Tools
=================================
Async tools that give the deep agent supervisor read access to the latest
LangGraph pipeline results stored in PostgreSQL.

Why these tools (not AGENTS.md or MCP):
  - LangGraph pipeline writes to DB after every run (inventory snapshots, alerts, etc.)
  - Deep agent needs these results to answer founder questions conversationally
  - AGENTS.md is for static brand context (rules, preferences) — not live operational data
  - Shopify MCP adds API latency; DB reads are instant (already processed + structured)

Design rules:
  - Every tool is brand_id scoped — no cross-tenant leaks
  - Return plain dicts/lists — no ORM objects (LLM can't reason over those)
  - Return empty lists / "no data" messages gracefully — never raise to the LLM
  - Docstrings are the routing signal — write them for the LLM, not for humans

Wire into supervisor:
  from deep_agents.tools.db_tools import get_db_tools
  agent = create_deep_agent(tools=get_db_tools(), ...)
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from db.models import (
    AgentRun,
    AlertRecord,
    InventorySnapshotRecord,
    PricingActionRecord,
    RestockRecommendationRecord,
    MarketingActionRecord,
    ContentPostRecord,
    ReturnInsightRecord,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
)

from langchain_core.tools import tool

# ── Session factory helper ─────────────────────────────────────────────────────
# NullPool = no persistent connections from the deep agent process.
# Each tool call opens + closes cleanly.

def _make_session() -> async_sessionmaker:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 1 — Pipeline status
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_pipeline_status(brand_id: str) -> dict:
    """
    Get the status of the last FashionOS agent pipeline run for this brand.
    Returns when it ran, which agents executed, how many alerts were generated,
    and the AI-written run summary. Call this first when the founder asks
    'what happened?' or 'when did FashionOS last run?' or 'give me an update'.

    Args:
        brand_id: The brand to query.

    Returns:
        {
            "last_run_at": "2025-06-10T03:05:00Z",
            "trigger": "scheduled_run",
            "agents_run": ["inventory", "trend", "pricing", ...],
            "alert_counts": {"critical": 2, "warning": 4, "total": 6},
            "summary": "AI-written summary of what happened",
            "inventory_skus_analysed": 24,
            "pricing_decisions": 8,
            "restock_orders": 3,
            "hours_ago": 4.2
        }
        or {"error": "No pipeline runs found for this brand yet."}
    """
    Session = _make_session()
    try:
        async with Session() as session:
            result = await session.execute(
                select(AgentRun)
                .where(AgentRun.brand_id == brand_id)
                .order_by(desc(AgentRun.created_at))
                .limit(1)
            )
            run = result.scalar_one_or_none()

            if not run:
                return {"error": "No pipeline runs found for this brand yet."}

            now = datetime.now(timezone.utc)
            completed = run.completed_at
            hours_ago = None
            if completed:
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=timezone.utc)
                hours_ago = round((now - completed).total_seconds() / 3600, 1)

            return {
                "last_run_at":           run.completed_at.isoformat() if run.completed_at else None,
                "trigger":               run.trigger,
                "agents_run":            run.agents_run or [],
                "alert_counts": {
                    "critical": run.alert_count_critical,
                    "warning":  run.alert_count_warning,
                    "total":    run.alert_count_total,
                },
                "summary":               run.run_summary,
                "inventory_skus_analysed": run.inventory_skus_analysed,
                "pricing_decisions":     run.pricing_decisions_total,
                "pricing_pending":       run.pricing_pending_approval,
                "marketing_decisions":   run.marketing_decisions_total,
                "marketing_pending":     run.marketing_pending_approval,
                "hours_ago":             hours_ago,
            }
    except Exception as exc:
        return {"error": f"DB error: {exc}"}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 2 — Full inventory snapshot
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_inventory_status(brand_id: str) -> dict:
    """
    Get the full inventory health snapshot from the latest pipeline run.
    Returns all SKUs with their stock levels, daily velocity, days remaining,
    and urgency classification. Call this when the founder asks about inventory,
    stock levels, which products are running low, or for a general health check.

    Args:
        brand_id: The brand to query.

    Returns:
        {
            "as_of": "2025-06-10T03:05:00Z",
            "total_skus": 24,
            "summary": {"critical": 2, "high": 1, "normal": 8, "healthy": 13},
            "skus": [
                {
                    "sku": "FOS-042-S",
                    "product": "Olive Cargo Pants",
                    "variant": "Small",
                    "stock": 8,
                    "velocity": 1.43,
                    "days_remaining": 5.6,
                    "urgency": "critical"
                },
                ...
            ]
        }
        Sorted by days_remaining ascending (most urgent first).
    """
    Session = _make_session()
    try:
        async with Session() as session:
            # Get the latest run_id for this brand
            run_result = await session.execute(
                select(AgentRun.run_id, AgentRun.completed_at)
                .where(AgentRun.brand_id == brand_id)
                .order_by(desc(AgentRun.created_at))
                .limit(1)
            )
            row = run_result.first()
            if not row:
                return {"error": "No pipeline runs found yet."}

            run_id, completed_at = row

            # Get all snapshots for that run
            snap_result = await session.execute(
                select(InventorySnapshotRecord)
                .where(InventorySnapshotRecord.run_id == run_id)
                .order_by(InventorySnapshotRecord.days_of_stock_remaining)
            )
            snaps = snap_result.scalars().all()

            if not snaps:
                return {"error": "No inventory data found for the latest run."}

            urgency_counts = {"critical": 0, "high": 0, "normal": 0, "healthy": 0}
            skus = []
            for s in snaps:
                urgency_counts[s.urgency] = urgency_counts.get(s.urgency, 0) + 1
                skus.append({
                    "sku":            s.sku,
                    "product":        s.product_title,
                    "variant":        s.variant_title,
                    "stock":          s.current_stock,
                    "velocity":       round(s.units_per_day, 2),
                    "days_remaining": s.days_of_stock_remaining,
                    "urgency":        s.urgency,
                })

            return {
                "as_of":      completed_at.isoformat() if completed_at else None,
                "total_skus": len(skus),
                "summary":    urgency_counts,
                "skus":       skus,
            }
    except Exception as exc:
        return {"error": f"DB error: {exc}"}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 3 — Critical SKUs only
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_critical_skus(brand_id: str) -> list[dict]:
    """
    Get only the SKUs classified as critical or high urgency from the latest run.
    Critical = less than 7 days of stock. High = 7-14 days.
    Call this when the founder asks 'what needs attention today?', 'what's about to
    run out?', or 'what should I restock urgently?'.

    Args:
        brand_id: The brand to query.

    Returns:
        List of urgent SKUs sorted by days_remaining ascending:
        [
            {
                "sku": "FOS-042-S",
                "product": "Olive Cargo Pants",
                "variant": "Small",
                "stock": 8,
                "velocity": 1.43,
                "days_remaining": 5.6,
                "urgency": "critical",
                "action": "Restock order must go TODAY"
            },
            ...
        ]
        Empty list if no urgent SKUs.
    """
    Session = _make_session()
    try:
        async with Session() as session:
            run_result = await session.execute(
                select(AgentRun.run_id)
                .where(AgentRun.brand_id == brand_id)
                .order_by(desc(AgentRun.created_at))
                .limit(1)
            )
            run_row = run_result.first()
            if not run_row:
                return []

            run_id = run_row[0]

            snap_result = await session.execute(
                select(InventorySnapshotRecord)
                .where(
                    InventorySnapshotRecord.run_id == run_id,
                    InventorySnapshotRecord.urgency.in_(["critical", "high"]),
                )
                .order_by(InventorySnapshotRecord.days_of_stock_remaining)
            )
            snaps = snap_result.scalars().all()

            results = []
            for s in snaps:
                action = (
                    "Restock order must go TODAY"
                    if s.urgency == "critical"
                    else "Order within 3 days"
                )
                results.append({
                    "sku":            s.sku,
                    "product":        s.product_title,
                    "variant":        s.variant_title,
                    "stock":          s.current_stock,
                    "velocity":       round(s.units_per_day, 2),
                    "days_remaining": s.days_of_stock_remaining,
                    "urgency":        s.urgency,
                    "action":         action,
                })

            return results
    except Exception as exc:
        return [{"error": f"DB error: {exc}"}]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 4 — Open alerts
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_open_alerts(brand_id: str, level: Optional[str] = None) -> list[dict]:
    """
    Get recent alerts raised by the FashionOS agents. These are the issues agents
    flagged that need founder awareness. Call this when the founder asks about
    problems, issues, warnings, or what the agents found. Use level='critical' to
    filter to only urgent alerts, 'warning' for warnings, or omit for all.

    Args:
        brand_id: The brand to query.
        level:    Optional filter — 'critical', 'warning', or 'info'.
                  If omitted, returns all levels.

    Returns:
        [
            {
                "level": "critical",
                "agent": "inventory_agent",
                "message": "FOS-042-S: 8 units, 5.6 days at 1.43/day. ORDER TODAY.",
                "sku": "FOS-042-S",
                "raised_at": "2025-06-10T03:05:00Z"
            },
            ...
        ]
        Sorted: critical first, then warning, then info. Within level: newest first.
    """
    Session = _make_session()
    try:
        async with Session() as session:
            query = (
                select(AlertRecord)
                .where(AlertRecord.brand_id == brand_id)
            )
            if level:
                query = query.where(AlertRecord.level == level)

            query = query.order_by(desc(AlertRecord.created_at)).limit(50)
            result = await session.execute(query)
            alerts = result.scalars().all()

            # Sort: critical → warning → info
            level_order = {"critical": 0, "warning": 1, "info": 2}
            sorted_alerts = sorted(alerts, key=lambda a: (level_order.get(a.level, 9), -a.created_at.timestamp()))

            return [
                {
                    "level":      a.level,
                    "agent":      a.agent,
                    "message":    a.message,
                    "sku":        a.sku,
                    "raised_at":  a.created_at.isoformat(),
                }
                for a in sorted_alerts
            ]
    except Exception as exc:
        return [{"error": f"DB error: {exc}"}]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 5 — All pending approvals (single call)
# ══════════════════════════════════════════════════════════════════════════════


@tool
async def get_pending_approvals(brand_id: str) -> dict:
    """
    Get everything currently waiting for the founder's approval in the dashboard.
    Returns counts and summaries for all approval queues: pricing decisions,
    restock orders, marketing budget changes, and content posts.
    Call this when the founder asks 'what needs my approval?', 'what's pending?',
    or 'what decisions are waiting for me?'.

    Args:
        brand_id: The brand to query.

    Returns:
        {
            "total_pending": 7,
            "pricing": [{"sku": "...", "action": "markdown", "discount_pct": 20, ...}],
            "restock": [{"sku": "...", "quantity": 80, "urgency": "critical", ...}],
            "marketing": [{"campaign": "...", "action": "increase_budget", ...}],
            "content": [{"sku": "...", "is_urgent": true, ...}]
        }
    """
    Session = _make_session()
    try:
        async with Session() as session:

            # Pending pricing (auto_executed=False, action != hold)
            pricing_result = await session.execute(
                select(PricingActionRecord)
                .where(
                    PricingActionRecord.brand_id     == brand_id,
                    PricingActionRecord.auto_executed == False,  # noqa: E712
                    PricingActionRecord.action        != "hold",
                    PricingActionRecord.action        != "rejected",
                )
                .order_by(desc(PricingActionRecord.discount_pct))
                .limit(20)
            )
            pricing = pricing_result.scalars().all()

            # Pending restock
            restock_result = await session.execute(
                select(RestockRecommendationRecord)
                .where(
                    RestockRecommendationRecord.brand_id == brand_id,
                    RestockRecommendationRecord.status   == "pending_approval",
                )
                .order_by(RestockRecommendationRecord.days_of_stock_remaining)
                .limit(20)
            )
            restock = restock_result.scalars().all()

            # Pending marketing (increase_budget / activate only)
            marketing_result = await session.execute(
                select(MarketingActionRecord)
                .where(
                    MarketingActionRecord.brand_id      == brand_id,
                    MarketingActionRecord.auto_executed == False,  # noqa: E712
                    MarketingActionRecord.action.in_(["increase_budget", "activate"]),
                )
                .order_by(desc(MarketingActionRecord.created_at))
                .limit(20)
            )
            marketing = marketing_result.scalars().all()

            # Pending content posts
            content_result = await session.execute(
                select(ContentPostRecord)
                .where(
                    ContentPostRecord.brand_id == brand_id,
                    ContentPostRecord.status   == "pending",
                )
                .order_by(desc(ContentPostRecord.is_urgent), desc(ContentPostRecord.created_at))
                .limit(20)
            )
            content = content_result.scalars().all()

            total = len(pricing) + len(restock) + len(marketing) + len(content)

            return {
                "total_pending": total,
                "pricing": [
                    {
                        "sku":              p.sku,
                        "action":           p.action,
                        "current_price":    p.current_price,
                        "recommended_price":p.recommended_price,
                        "discount_pct":     p.discount_pct,
                        "reason":           p.reason,
                    }
                    for p in pricing
                ],
                "restock": [
                    {
                        "sku":              r.sku,
                        "quantity":         r.recommended_quantity,
                        "urgency":          r.urgency,
                        "days_remaining":   r.days_of_stock_remaining,
                        "velocity":         round(r.units_per_day, 2),
                        "reason":           r.reason,
                    }
                    for r in restock
                ],
                "marketing": [
                    {
                        "campaign":         m.campaign_name,
                        "campaign_id":      m.campaign_id,
                        "action":           m.action,
                        "current_budget":   m.current_budget_pkr,
                        "new_budget":       m.new_budget_pkr,
                        "reason":           m.reason,
                    }
                    for m in marketing
                ],
                "content": [
                    {
                        "sku":          c.sku,
                        "product":      c.product_title,
                        "is_urgent":    c.is_urgent,
                        "has_instagram":c.instagram_caption is not None,
                        "has_tiktok":   c.tiktok_script is not None,
                        "post_time":    c.instagram_post_time,
                    }
                    for c in content
                ],
            }
    except Exception as exc:
        return {"error": f"DB error: {exc}", "total_pending": 0}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 6 — SKU history (time series)
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_sku_history(brand_id: str, sku: str) -> dict:
    """
    Get the inventory history for a specific SKU across multiple pipeline runs.
    Shows how stock and velocity have changed over time. Call this when the founder
    asks about a specific product, its trend, how fast it's selling, or its stock
    history. Example triggers: 'tell me about FOS-042-S', 'how is the olive cargo
    pants doing?', 'show me the velocity trend for this SKU'.

    Args:
        brand_id: The brand to query.
        sku:      The exact SKU string to look up (case-sensitive).

    Returns:
        {
            "sku": "FOS-042-S",
            "current": { latest snapshot },
            "history": [ older snapshots, newest first ],
            "trend": "declining" | "stable" | "accelerating"
        }
    """
    Session = _make_session()
    try:
        async with Session() as session:
            result = await session.execute(
                select(InventorySnapshotRecord)
                .where(
                    InventorySnapshotRecord.brand_id == brand_id,
                    InventorySnapshotRecord.sku      == sku,
                )
                .order_by(desc(InventorySnapshotRecord.created_at))
                .limit(30)
            )
            snaps = result.scalars().all()

            if not snaps:
                return {"error": f"No history found for SKU '{sku}'. Check the SKU spelling."}

            def _fmt(s: InventorySnapshotRecord) -> dict:
                return {
                    "stock":          s.current_stock,
                    "velocity":       round(s.units_per_day, 2),
                    "days_remaining": s.days_of_stock_remaining,
                    "urgency":        s.urgency,
                    "recorded_at":    s.created_at.isoformat(),
                }

            current  = _fmt(snaps[0])
            history  = [_fmt(s) for s in snaps[1:]]

            # Simple trend: compare latest velocity to 3 runs ago
            trend = "stable"
            if len(snaps) >= 4:
                latest_v    = snaps[0].units_per_day
                earlier_v   = snaps[3].units_per_day
                if latest_v > earlier_v * 1.2:
                    trend = "accelerating"
                elif latest_v < earlier_v * 0.8:
                    trend = "declining"

            return {
                "sku":     sku,
                "product": snaps[0].product_title,
                "variant": snaps[0].variant_title,
                "current": current,
                "history": history,
                "trend":   trend,
            }
    except Exception as exc:
        return {"error": f"DB error: {exc}"}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 7 — Return insights
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_return_insights(brand_id: str) -> list[dict]:
    """
    Get structured return patterns identified by the Returns Agent. Shows which
    products have high return rates, why customers are returning them, and what
    fix is recommended. Call this when the founder asks about returns, why a product
    is being returned, or what to fix to reduce return rate.

    Args:
        brand_id: The brand to query.

    Returns:
        [
            {
                "sku": "FOS-017-M",
                "product": "Beige Linen Dress",
                "total_returns": 12,
                "return_rate_pct": 18.5,
                "primary_reason": "size_issue",
                "severity": "critical",
                "recommended_fix": "Add cm measurements to size guide...",
                "fix_type": "update_size_guide"
            },
            ...
        ]
        Sorted: critical first.
    """
    Session = _make_session()
    try:
        async with Session() as session:
            result = await session.execute(
                select(ReturnInsightRecord)
                .where(ReturnInsightRecord.brand_id == brand_id)
                .order_by(desc(ReturnInsightRecord.created_at))
                .limit(30)
            )
            insights = result.scalars().all()

            if not insights:
                return [{"message": "No return insights found. Returns Agent may not have run yet."}]

            severity_order = {"critical": 0, "warning": 1, "info": 2}
            sorted_insights = sorted(insights, key=lambda i: severity_order.get(i.severity, 9))

            return [
                {
                    "sku":              i.sku,
                    "product":          i.product_title,
                    "total_returns":    i.total_returns,
                    "units_returned":   i.total_units_returned,
                    "return_rate_pct":  i.return_rate_pct,
                    "primary_reason":   i.primary_reason,
                    "severity":         i.severity,
                    "recommended_fix":  i.recommended_fix,
                    "fix_type":         i.fix_type,
                }
                for i in sorted_insights
            ]
    except Exception as exc:
        return [{"error": f"DB error: {exc}"}]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 8 — Content queue
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_content_queue(brand_id: str) -> list[dict]:
    """
    Get pending content posts generated by the Content Agent — Instagram captions,
    TikTok scripts, optimal posting times. Call this when the founder asks what to
    post today, what content is ready, or what to film for TikTok.

    Args:
        brand_id: The brand to query.

    Returns:
        [
            {
                "sku": "FOS-042-S",
                "product": "Olive Cargo Pants",
                "is_urgent": true,
                "instagram_caption": "...",
                "instagram_hashtags": [...],
                "instagram_post_time": "8:00 PM",
                "tiktok_script": { "hook": "...", "context": "...", ... },
                "tiktok_post_time": "7:30 PM",
                "creator_notes": "..."
            },
            ...
        ]
        Urgent posts first.
    """
    Session = _make_session()
    try:
        async with Session() as session:
            result = await session.execute(
                select(ContentPostRecord)
                .where(
                    ContentPostRecord.brand_id == brand_id,
                    ContentPostRecord.status   == "pending",
                )
                .order_by(desc(ContentPostRecord.is_urgent), desc(ContentPostRecord.created_at))
                .limit(20)
            )
            posts = result.scalars().all()

            if not posts:
                return [{"message": "No pending content posts. Content Agent may not have run yet."}]

            return [
                {
                    "sku":                  p.sku,
                    "product":              p.product_title,
                    "variant":              p.variant_title,
                    "is_urgent":            p.is_urgent,
                    "instagram_caption":    p.instagram_caption,
                    "instagram_hashtags":   p.instagram_hashtags,
                    "instagram_post_time":  p.instagram_post_time,
                    "tiktok_script":        p.tiktok_script,
                    "tiktok_post_time":     p.tiktok_post_time,
                    "creator_notes":        p.creator_notes,
                    "sale_mention":         p.sale_mention,
                }
                for p in posts
            ]
    except Exception as exc:
        return [{"error": f"DB error: {exc}"}]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL 9 — Run history
# ══════════════════════════════════════════════════════════════════════════════

@tool
async def get_run_history(brand_id: str, limit: int = 7) -> list[dict]:
    """
    Get the last N pipeline run summaries. Shows the pattern of when FashionOS ran,
    what it found, and how alert counts changed over time. Call this when the founder
    asks 'how has the store been doing this week?' or 'show me recent activity'.

    Args:
        brand_id: The brand to query.
        limit:    Number of recent runs to return (default 7 = last week's daily runs).

    Returns:
        [
            {
                "ran_at": "2025-06-10T03:05:00Z",
                "trigger": "scheduled_run",
                "agents_run": ["inventory", "trend", ...],
                "critical_alerts": 2,
                "warning_alerts": 4,
                "summary": "2 SKUs critical..."
            },
            ...
        ]
        Newest first.
    """
    Session = _make_session()
    try:
        async with Session() as session:
            result = await session.execute(
                select(AgentRun)
                .where(AgentRun.brand_id == brand_id)
                .order_by(desc(AgentRun.created_at))
                .limit(limit)
            )
            runs = result.scalars().all()

            if not runs:
                return [{"message": "No runs found yet."}]

            return [
                {
                    "ran_at":          r.completed_at.isoformat() if r.completed_at else None,
                    "trigger":         r.trigger,
                    "agents_run":      r.agents_run or [],
                    "critical_alerts": r.alert_count_critical,
                    "warning_alerts":  r.alert_count_warning,
                    "skus_analysed":   r.inventory_skus_analysed,
                    "summary":         r.run_summary,
                }
                for r in runs
            ]
    except Exception as exc:
        return [{"error": f"DB error: {exc}"}]


# ══════════════════════════════════════════════════════════════════════════════
# Tool registry — pass to create_deep_agent(tools=get_db_tools())
# ══════════════════════════════════════════════════════════════════════════════

def get_db_tools() -> list:
    """
    Returns all DB tools as a list ready to pass to create_deep_agent().

    Usage:
        from deep_agents.tools.db_tools import get_db_tools
        agent = create_deep_agent(
            model="google_genai:gemini-2.5-flash-lite",
            tools=get_db_tools(),
            ...
        )
    """
    return [
        get_pipeline_status,
        get_inventory_status,
        get_critical_skus,
        get_open_alerts,
        get_pending_approvals,
        get_sku_history,
        get_return_insights,
        get_content_queue,
        get_run_history,
    ]