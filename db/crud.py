"""
FashionOS CRUD Operations
==========================
All database reads and writes. No business logic — pure I/O.

Session 6 additions:
  save_run()  — now persists marketing_actions, content_posts, return_insights
  get_pending_marketing()  → marketing approval queue
  get_content_queue()      → pending content posts (newest first)
  get_return_insights()    → returns fix queue (most severe first)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AgentRun,
    AlertRecord,
    ContentPostRecord,
    InventorySnapshotRecord,
    MarketingActionRecord,
    PricingActionRecord,
    RestockRecommendationRecord,
    ReturnInsightRecord,
)

from sqlalchemy import update as sa_update 

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — save_run
# ══════════════════════════════════════════════════════════════════════════════

async def save_run(
    session: AsyncSession,
    summary: dict,
    state:   dict,
) -> Optional[AgentRun]:
    """
    Persists a completed agent pipeline run and all child records.

    Idempotent — if run_id already exists (Celery retry), logs a warning and
    returns None without duplicating rows.

    Child records saved:
      inventory_snapshots, pricing_actions, alerts, restock_recommendations,
      marketing_actions (NEW), content_posts (NEW), return_insights (NEW)
    """
    run_id = summary["run_id"]

    # ── Idempotency guard ─────────────────────────────────────────────────────
    existing = await session.execute(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    if existing.scalar_one_or_none():
        logger.warning(f"[DB] run_id={run_id} already in DB — skipping duplicate save.")
        return None

    # ── Marketing stats for cached columns ────────────────────────────────────
    marketing_stats   = summary.get("marketing", {})
    alert_counts      = summary.get("alert_counts", {})
    pricing_stats     = summary.get("pricing", {})

    # ── 1. AgentRun (parent row) ──────────────────────────────────────────────
    run = AgentRun(
        run_id               = run_id,
        brand_id             = summary["brand_id"],
        brand_name           = summary["brand_name"],
        trigger              = summary["trigger"],
        trigger_payload      = state.get("trigger_payload"),
        task_id              = summary.get("task_id"),
        started_at           = _parse_dt(summary.get("started_at")) or datetime.now(timezone.utc),
        completed_at         = _parse_dt(summary.get("completed_at") or state.get("completed_at")),
        agents_run           = summary.get("completed_agents", []),
        run_summary          = summary.get("run_summary"),
        supervisor_reasoning = state.get("supervisor_reasoning"),

        alert_count_critical     = alert_counts.get("critical", 0),
        alert_count_warning      = alert_counts.get("warning", 0),
        alert_count_total        = alert_counts.get("total", 0),
        inventory_skus_analysed  = summary.get("inventory_skus_analysed", 0),
        pricing_decisions_total  = pricing_stats.get("total_decisions", 0),
        pricing_auto_executed    = pricing_stats.get("auto_executed", 0),
        pricing_pending_approval = pricing_stats.get("pending_approval", 0),

        # Marketing cached counts (NEW session 6)
        marketing_decisions_total  = marketing_stats.get("total_decisions", 0),
        marketing_auto_executed    = marketing_stats.get("auto_executed", 0),
        marketing_pending_approval = marketing_stats.get("pending_approval", 0),
    )
    session.add(run)

    # ── 2. Inventory snapshots ────────────────────────────────────────────────
    for snap in state.get("inventory_snapshot", []):
        session.add(InventorySnapshotRecord(
            run_id                  = run_id,
            brand_id                = summary["brand_id"],
            sku                     = snap.get("sku", ""),
            product_title           = snap.get("product_title", ""),
            variant_title           = snap.get("variant_title", ""),
            current_stock           = snap.get("current_stock", 0),
            units_per_day           = snap.get("units_per_day", 0.0),
            days_of_stock_remaining = snap.get("days_of_stock_remaining", 999.0),
            urgency                 = snap.get("urgency", "healthy"),
        ))

    # ── 3. Pricing actions ────────────────────────────────────────────────────
    for rec in state.get("pricing_recommendations", []):
        action  = rec.get("action", "hold")
        is_auto = (action == "hold") or (
            action == "markdown" and rec.get("discount_pct", 0) <= 15
        )
        session.add(PricingActionRecord(
            run_id            = run_id,
            brand_id          = summary["brand_id"],
            sku               = rec.get("sku", ""),
            variant_id        = rec.get("variant_id"),
            action            = action,
            current_price     = rec.get("current_price", 0.0),
            recommended_price = rec.get("recommended_price", 0.0),
            discount_pct      = rec.get("discount_pct", 0.0),
            auto_executed     = is_auto,
            reason            = rec.get("reason"),
        ))

    # ── 4. Alerts ─────────────────────────────────────────────────────────────
    for alert in state.get("alerts", []):
        created_at = _parse_dt(alert.get("created_at")) or datetime.now(timezone.utc)
        session.add(AlertRecord(
            run_id     = run_id,
            brand_id   = summary["brand_id"],
            level      = alert.get("level", "info"),
            agent      = alert.get("agent", "unknown"),
            message    = alert.get("message", ""),
            sku        = alert.get("sku"),
            created_at = created_at,
        ))

    # ── 5. Restock recommendations ────────────────────────────────────────────
    for rec in state.get("restock_recommendations", []):
        session.add(RestockRecommendationRecord(
            run_id                  = run_id,
            brand_id                = summary["brand_id"],
            sku                     = rec.get("sku", ""),
            recommended_quantity    = rec.get("recommended_quantity", 0),
            urgency                 = rec.get("urgency", "normal"),
            days_of_stock_remaining = rec.get("days_of_stock_remaining", 0.0),
            units_per_day           = rec.get("units_per_day", 0.0),
            reason                  = rec.get("reason", ""),
            supplier_message        = rec.get("supplier_message", ""),
            status                  = rec.get("status", "pending_approval"),
        ))

    # ── 6. Marketing actions (NEW session 6) ──────────────────────────────────
    for act in state.get("marketing_actions", []):
        session.add(MarketingActionRecord(
            run_id             = run_id,
            brand_id           = summary["brand_id"],
            sku                = act.get("sku") or None,
            campaign_id        = act.get("campaign_id", ""),
            campaign_name      = act.get("campaign_name", ""),
            action             = act.get("action", "hold"),
            current_budget_pkr = abs(act.get("amount_delta", 0.0) or 0.0),  # fallback
            new_budget_pkr     = None,   # amount_delta is relative; absolute set separately
            change_pct         = 0.0,    # derived from amount_delta if needed
            auto_executed      = act.get("auto_executed", False),
            reason             = act.get("reason"),
            trigger            = act.get("trigger"),
        ))

    # ── 7. Content posts (NEW session 6) ──────────────────────────────────────
    for post in state.get("content_queue", []):
        ig   = post.get("instagram", {})
        tt   = post.get("tiktok", {})
        session.add(ContentPostRecord(
            run_id             = run_id,
            brand_id           = summary["brand_id"],
            sku                = post.get("sku", ""),
            product_title      = post.get("product_title", ""),
            variant_title      = post.get("variant_title", ""),
            is_urgent          = post.get("is_urgent", False),
            status             = post.get("status", "pending"),
            instagram_caption  = ig.get("caption"),
            instagram_hashtags = ig.get("hashtags"),
            instagram_post_time= ig.get("optimal_time"),
            tiktok_script      = tt.get("script"),
            tiktok_post_time   = tt.get("optimal_time"),
            creator_notes      = post.get("creator_notes"),
            sale_mention       = post.get("sale_mention"),
        ))

    # ── 8. Return insights (NEW session 6) ────────────────────────────────────
    for insight in state.get("return_insights", []):
        session.add(ReturnInsightRecord(
            run_id               = run_id,
            brand_id             = summary["brand_id"],
            sku                  = insight.get("sku", ""),
            product_title        = insight.get("product_title", ""),
            total_returns        = insight.get("total_returns", 0),
            total_units_returned = insight.get("total_units_returned", 0),
            primary_reason       = insight.get("primary_reason", "other"),
            return_rate_pct      = insight.get("return_rate_pct"),
            estimated_30d_sales  = insight.get("estimated_30d_sales"),
            severity             = insight.get("severity", "info"),
            recommended_fix      = insight.get("recommended_fix", ""),
            fix_type             = insight.get("fix_type", "monitor"),
        ))

    logger.info(
        f"[DB] Queued run={run_id} | "
        f"snapshots={len(state.get('inventory_snapshot', []))} | "
        f"pricing={len(state.get('pricing_recommendations', []))} | "
        f"alerts={len(state.get('alerts', []))} | "
        f"marketing={len(state.get('marketing_actions', []))} | "
        f"content={len(state.get('content_queue', []))} | "
        f"return_insights={len(state.get('return_insights', []))}"
    )
    return run


# ══════════════════════════════════════════════════════════════════════════════
# READS — Existing helpers (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

async def list_runs(
    session: AsyncSession,
    brand_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRun]:
    result = await session.execute(
        select(AgentRun)
        .where(AgentRun.brand_id == brand_id)
        .order_by(desc(AgentRun.created_at))
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_run(session: AsyncSession, run_id: str) -> Optional[AgentRun]:
    result = await session.execute(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    return result.scalar_one_or_none()


async def get_run_snapshots(
    session: AsyncSession, run_id: str
) -> list[InventorySnapshotRecord]:
    result = await session.execute(
        select(InventorySnapshotRecord)
        .where(InventorySnapshotRecord.run_id == run_id)
        .order_by(InventorySnapshotRecord.days_of_stock_remaining)
    )
    return list(result.scalars().all())


async def get_run_pricing(
    session: AsyncSession, run_id: str
) -> list[PricingActionRecord]:
    result = await session.execute(
        select(PricingActionRecord)
        .where(PricingActionRecord.run_id == run_id)
        .order_by(desc(PricingActionRecord.discount_pct))
    )
    return list(result.scalars().all())


async def get_run_alerts(
    session: AsyncSession, run_id: str
) -> list[AlertRecord]:
    result = await session.execute(
        select(AlertRecord).where(AlertRecord.run_id == run_id)
    )
    return list(result.scalars().all())


async def get_pending_pricing(
    session: AsyncSession,
    brand_id: str,
    limit: int = 100,
) -> list[PricingActionRecord]:
    result = await session.execute(
        select(PricingActionRecord)
        .where(
            PricingActionRecord.brand_id     == brand_id,
            PricingActionRecord.auto_executed == False,   # noqa: E712
            PricingActionRecord.action        != "hold",
        )
        .order_by(desc(PricingActionRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_pending_restocks(
    session: AsyncSession,
    brand_id: str,
) -> list[RestockRecommendationRecord]:
    result = await session.execute(
        select(RestockRecommendationRecord)
        .where(
            RestockRecommendationRecord.brand_id == brand_id,
            RestockRecommendationRecord.status   == "pending_approval",
        )
        .order_by(RestockRecommendationRecord.days_of_stock_remaining)
    )
    return list(result.scalars().all())


async def get_critical_alerts(
    session: AsyncSession,
    brand_id: str,
    limit: int = 20,
) -> list[AlertRecord]:
    result = await session.execute(
        select(AlertRecord)
        .where(
            AlertRecord.brand_id == brand_id,
            AlertRecord.level    == "critical",
        )
        .order_by(desc(AlertRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_sku_history(
    session: AsyncSession,
    brand_id: str,
    sku: str,
    limit: int = 30,
) -> list[InventorySnapshotRecord]:
    result = await session.execute(
        select(InventorySnapshotRecord)
        .where(
            InventorySnapshotRecord.brand_id == brand_id,
            InventorySnapshotRecord.sku      == sku,
        )
        .order_by(desc(InventorySnapshotRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


# ══════════════════════════════════════════════════════════════════════════════
# READS — New helpers (session 6)
# ══════════════════════════════════════════════════════════════════════════════

async def get_pending_marketing(
    session: AsyncSession,
    brand_id: str,
    limit: int = 100,
) -> list[MarketingActionRecord]:
    """
    Marketing decisions awaiting human approval (increase_budget, activate).
    Sorted by most recent run first — so the dashboard always shows latest decisions.
    """
    result = await session.execute(
        select(MarketingActionRecord)
        .where(
            MarketingActionRecord.brand_id      == brand_id,
            MarketingActionRecord.auto_executed == False,   # noqa: E712
            MarketingActionRecord.action.in_(["increase_budget", "activate"]),
        )
        .order_by(desc(MarketingActionRecord.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_content_queue(
    session: AsyncSession,
    brand_id: str,
    status: str = "pending",
    limit: int = 50,
) -> list[ContentPostRecord]:
    """
    Content posts with the given status (default: pending).
    Urgent posts first, then by creation time.
    """
    result = await session.execute(
        select(ContentPostRecord)
        .where(
            ContentPostRecord.brand_id == brand_id,
            ContentPostRecord.status   == status,
        )
        .order_by(
            desc(ContentPostRecord.is_urgent),
            desc(ContentPostRecord.created_at),
        )
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_return_insights(
    session: AsyncSession,
    brand_id: str,
    severity: Optional[str] = None,
    limit: int = 50,
) -> list[ReturnInsightRecord]:
    """
    Returns fix queue. Critical first, then warning, then info.
    Optionally filter by severity.
    """
    severity_order = {"critical": 0, "warning": 1, "info": 2}

    query = (
        select(ReturnInsightRecord)
        .where(ReturnInsightRecord.brand_id == brand_id)
    )
    if severity:
        query = query.where(ReturnInsightRecord.severity == severity)

    # Sort by severity (critical first) then created_at desc
    query = query.order_by(
        desc(ReturnInsightRecord.created_at)
    ).limit(limit)

    result = await session.execute(query)
    rows   = list(result.scalars().all())

    # Python-side sort by severity priority (SQL CASE would also work)
    rows.sort(key=lambda r: (severity_order.get(r.severity, 9), r.created_at), reverse=False)
    return rows


async def get_run_marketing(
    session: AsyncSession, run_id: str
) -> list[MarketingActionRecord]:
    """All marketing actions for a specific run."""
    result = await session.execute(
        select(MarketingActionRecord)
        .where(MarketingActionRecord.run_id == run_id)
        .order_by(desc(MarketingActionRecord.created_at))
    )
    return list(result.scalars().all())


async def get_run_content(
    session: AsyncSession, run_id: str
) -> list[ContentPostRecord]:
    """All content posts for a specific run. Urgent first."""
    result = await session.execute(
        select(ContentPostRecord)
        .where(ContentPostRecord.run_id == run_id)
        .order_by(desc(ContentPostRecord.is_urgent), ContentPostRecord.created_at)
    )
    return list(result.scalars().all())


async def get_run_return_insights(
    session: AsyncSession, run_id: str
) -> list[ReturnInsightRecord]:
    """All return insights for a specific run."""
    result = await session.execute(
        select(ReturnInsightRecord)
        .where(ReturnInsightRecord.run_id == run_id)
    )
    return list(result.scalars().all())


# ── Helper ─────────────────────────────────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
    
# ----------------
# session 7 updation 
# ---------------
    

async def get_pricing_action(
    session: AsyncSession,
    record_id: str,
) -> Optional[PricingActionRecord]:
    result = await session.execute(
        select(PricingActionRecord).where(PricingActionRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def get_marketing_action(
    session: AsyncSession,
    record_id: str,
) -> Optional[MarketingActionRecord]:
    result = await session.execute(
        select(MarketingActionRecord).where(MarketingActionRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def get_restock_recommendation(
    session: AsyncSession,
    record_id: str,
) -> Optional[RestockRecommendationRecord]:
    result = await session.execute(
        select(RestockRecommendationRecord).where(RestockRecommendationRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def get_content_post(
    session: AsyncSession,
    record_id: str,
) -> Optional[ContentPostRecord]:
    result = await session.execute(
        select(ContentPostRecord).where(ContentPostRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def mark_pricing_executed(
    session: AsyncSession,
    record_id: str,
) -> Optional[PricingActionRecord]:
    """Set auto_executed=True on a pricing action record."""
    await session.execute(
        sa_update(PricingActionRecord)
        .where(PricingActionRecord.id == record_id)
        .values(auto_executed=True)
    )
    await session.flush()
    return await get_pricing_action(session, record_id)


async def mark_pricing_rejected(
    session: AsyncSession,
    record_id: str,
) -> Optional[PricingActionRecord]:
    """Set action='rejected' so the dashboard can filter it out."""
    await session.execute(
        sa_update(PricingActionRecord)
        .where(PricingActionRecord.id == record_id)
        .values(action="rejected")
    )
    await session.flush()
    return await get_pricing_action(session, record_id)


async def mark_marketing_executed(
    session: AsyncSession,
    record_id: str,
) -> Optional[MarketingActionRecord]:
    """Set auto_executed=True on a marketing action record."""
    await session.execute(
        sa_update(MarketingActionRecord)
        .where(MarketingActionRecord.id == record_id)
        .values(auto_executed=True)
    )
    await session.flush()
    return await get_marketing_action(session, record_id)


async def mark_marketing_rejected(
    session: AsyncSession,
    record_id: str,
) -> Optional[MarketingActionRecord]:
    """Set action='rejected' on a marketing action record."""
    await session.execute(
        sa_update(MarketingActionRecord)
        .where(MarketingActionRecord.id == record_id)
        .values(action="rejected")
    )
    await session.flush()
    return await get_marketing_action(session, record_id)


async def update_restock_status(
    session: AsyncSession,
    record_id: str,
    new_status: str,   # "approved" | "ordered" | "cancelled"
) -> Optional[RestockRecommendationRecord]:
    """Update restock recommendation status."""
    await session.execute(
        sa_update(RestockRecommendationRecord)
        .where(RestockRecommendationRecord.id == record_id)
        .values(status=new_status)
    )
    await session.flush()
    return await get_restock_recommendation(session, record_id)


async def update_content_post_status(
    session: AsyncSession,
    record_id: str,
    new_status: str,   # "posted" | "skipped"
) -> Optional[ContentPostRecord]:
    """Update content post status."""
    await session.execute(
        sa_update(ContentPostRecord)
        .where(ContentPostRecord.id == record_id)
        .values(status=new_status)
    )
    await session.flush()
    return await get_content_post(session, record_id)