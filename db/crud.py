"""
FashionOS CRUD Operations
==========================
All database reads and writes. No business logic — pure I/O.

Session 6 additions:
  save_run()  — now persists marketing_actions, content_posts, return_insights
  get_pending_marketing()  → marketing approval queue
  get_content_queue()      → pending content posts (newest first)
  get_return_insights()    → returns fix queue (most severe first)

Session 8 additions (multi-tenancy hardening):
  All approval/update helpers now accept brand_id and include it in the WHERE
  clause as a second ownership guard — defense in depth against cross-tenant writes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AgentRun,
    AlertRecord,
    ContentPostRecord,
    DMReplyRecord,
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
      marketing_actions, content_posts, return_insights
    """
    run_id = summary["run_id"]

    # ── Idempotency guard ─────────────────────────────────────────────────────
    existing = await session.execute(
        select(AgentRun).where(AgentRun.run_id == run_id)
    )
    if existing.scalar_one_or_none():
        logger.warning(f"[DB] run_id={run_id} already in DB — skipping duplicate save.")
        return None

    # ── Marketing / DM stats for cached columns ───────────────────────────────
    marketing_stats   = summary.get("marketing", {})
    alert_counts      = summary.get("alert_counts", {})
    pricing_stats     = summary.get("pricing", {})
    dm_stats          = summary.get("dm", {})

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

        marketing_decisions_total  = marketing_stats.get("total_decisions", 0),
        marketing_auto_executed    = marketing_stats.get("auto_executed", 0),
        marketing_pending_approval = marketing_stats.get("pending_approval", 0),

        dm_auto_replied = dm_stats.get("auto_replied", 0),
        dm_flagged_open = dm_stats.get("flagged", 0),
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
            velocity_7d                        = snap.get("velocity_7d", 0.0),
            velocity_30d                       = snap.get("velocity_30d", 0.0),
            velocity_trend                     = snap.get("velocity_trend", "stable"),
            velocity_confidence                = snap.get("velocity_confidence", "low"),
            seasonal_multiplier_applied        = snap.get("seasonal_multiplier_applied", 1.0),
            seasonal_context                   = snap.get("seasonal_context", "off_season"),
            days_of_stock_remaining_unadjusted = snap.get("days_of_stock_remaining_unadjusted", 999.0),
            reorder_point_units                = snap.get("reorder_point_units", 0),
            has_pending_restock                = snap.get("has_pending_restock", False),
            pending_restock_note               = snap.get("pending_restock_note"),
            size_curve_deviation                = snap.get("size_curve_deviation", False),
            size_curve_note                     = snap.get("size_curve_note"),
        ))

    # ── 3. Pricing actions ────────────────────────────────────────────────────
    for rec in state.get("pricing_recommendations", []):
        session.add(PricingActionRecord(
            run_id            = run_id,
            brand_id          = summary["brand_id"],
            sku               = rec.get("sku", ""),
            variant_id        = rec.get("variant_id"),
            action            = rec.get("action", "hold"),
            current_price     = rec.get("current_price", 0.0),
            recommended_price = rec.get("recommended_price", 0.0),
            discount_pct      = rec.get("discount_pct", 0.0),
            auto_executed     = rec.get("auto_executed", False),
            reason            = rec.get("reason"),
            trigger                  = rec.get("trigger", "healthy"),
            markdown_rung             = rec.get("markdown_rung", 0),
            estimated_unit_cost_pkr   = rec.get("estimated_unit_cost_pkr"),
            estimated_margin_pct      = rec.get("estimated_margin_pct"),
            suggested_discount_code   = rec.get("suggested_discount_code"),
            new_compare_at_price       = rec.get("new_compare_at_price"),
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
            run_id                    = run_id,
            brand_id                  = summary["brand_id"],
            sku                       = rec.get("sku", ""),
            recommended_quantity      = rec.get("recommended_quantity", 0),
            urgency                   = rec.get("urgency", "normal"),
            days_of_stock_remaining   = rec.get("days_of_stock_remaining", 0.0),
            units_per_day             = rec.get("units_per_day", 0.0),
            reason                    = rec.get("reason", ""),
            supplier_message          = rec.get("supplier_message", ""),
            status                    = rec.get("status", "pending_approval"),
            supplier_type             = rec.get("supplier_type", "lahore_local"),
            estimated_lead_days       = rec.get("estimated_lead_days", 0),
            expected_stockout_date    = _parse_date(rec.get("expected_stockout_date")),
            order_deadline            = _parse_date(rec.get("order_deadline")),
            is_overdue                = rec.get("is_overdue", False),
            estimated_unit_cost_pkr   = rec.get("estimated_unit_cost_pkr"),
            estimated_total_cost_pkr  = rec.get("estimated_total_cost_pkr"),
            priority                  = rec.get("priority", 0),
        ))

    # ── 6. Marketing actions ──────────────────────────────────────────────────
    for act in state.get("marketing_actions", []):
        session.add(MarketingActionRecord(
            run_id             = run_id,
            brand_id           = summary["brand_id"],
            sku                = act.get("sku") or None,
            campaign_id        = act.get("campaign_id", ""),
            campaign_name      = act.get("campaign_name", ""),
            action             = act.get("action", "hold"),
            current_budget_pkr = act.get("current_budget_pkr", 0.0),
            new_budget_pkr     = act.get("new_budget_pkr"),
            change_pct         = act.get("change_pct", 0.0),
            auto_executed      = act.get("auto_executed", False),
            reason             = act.get("reason"),
            trigger            = act.get("trigger"),
            roas_7d            = act.get("roas_7d"),
            spend_7d_pkr       = act.get("spend_7d_pkr", 0.0),
            ctr_7d             = act.get("ctr_7d", 0.0),
        ))

    # ── 7. Content posts ──────────────────────────────────────────────────────
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
            trigger            = post.get("trigger", "on_sale"),
            trend_score        = post.get("trend_score"),
            discount_pct       = post.get("discount_pct", 0.0),
            instagram_caption  = ig.get("caption"),
            instagram_hashtags = ig.get("hashtags"),
            instagram_post_time= ig.get("optimal_time"),
            tiktok_script      = tt.get("script"),
            tiktok_post_time   = tt.get("optimal_time"),
            creator_notes      = post.get("creator_notes"),
            sale_mention       = post.get("sale_mention"),
        ))

    # ── 8. Return insights ────────────────────────────────────────────────────
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
            reason_breakdown     = insight.get("reason_breakdown"),
            evidence             = insight.get("evidence"),
        ))

    # ── 9. DM replies (spam not persisted — noise reduction) ─────────────────
    for reply in state.get("dm_replies", []):
        session.add(DMReplyRecord(
            run_id            = run_id,
            brand_id          = summary["brand_id"],
            message_id        = reply.get("message_id", ""),
            conversation_id   = reply.get("conversation_id", ""),
            user_id           = reply.get("user_id", ""),
            username          = reply.get("username", ""),
            original_message  = reply.get("original_message", ""),
            category          = reply.get("category", "general_inquiry"),
            auto_send         = reply.get("auto_send", False),
            flag_for_human    = reply.get("flag_for_human", False),
            flag_priority     = reply.get("flag_priority"),
            flag_reason       = reply.get("flag_reason"),
            reply_text        = reply.get("reply_text"),
            auto_sent         = reply.get("auto_sent", False),
            sent_at           = _parse_dt(reply.get("sent_at")),
            status            = reply.get("status", "flagged_open"),
        ))

    logger.info(
        f"[DB] Queued run={run_id} | "
        f"snapshots={len(state.get('inventory_snapshot', []))} | "
        f"pricing={len(state.get('pricing_recommendations', []))} | "
        f"alerts={len(state.get('alerts', []))} | "
        f"marketing={len(state.get('marketing_actions', []))} | "
        f"content={len(state.get('content_queue', []))} | "
        f"return_insights={len(state.get('return_insights', []))} | "
        f"dm_replies={len(state.get('dm_replies', []))}"
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


async def get_restocks_in_flight(
    session: AsyncSession,
    brand_id: str,
) -> list[RestockRecommendationRecord]:
    """
    SKUs with a restock already pending_approval, approved, or ordered.
    Used by the Inventory Agent to avoid re-raising a duplicate critical
    alert on a SKU that's already been handled.
    """
    result = await session.execute(
        select(RestockRecommendationRecord)
        .where(
            RestockRecommendationRecord.brand_id == brand_id,
            RestockRecommendationRecord.status.in_(["pending_approval", "approved", "ordered"]),
        )
        .order_by(desc(RestockRecommendationRecord.created_at))
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
        .order_by(RestockRecommendationRecord.priority, RestockRecommendationRecord.days_of_stock_remaining)
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


async def get_pending_marketing(
    session: AsyncSession,
    brand_id: str,
    limit: int = 100,
) -> list[MarketingActionRecord]:
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
    severity_order = {"critical": 0, "warning": 1, "info": 2}

    query = (
        select(ReturnInsightRecord)
        .where(ReturnInsightRecord.brand_id == brand_id)
    )
    if severity:
        query = query.where(ReturnInsightRecord.severity == severity)

    query = query.order_by(desc(ReturnInsightRecord.created_at)).limit(limit)

    result = await session.execute(query)
    rows   = list(result.scalars().all())
    rows.sort(key=lambda r: (severity_order.get(r.severity, 9), r.created_at), reverse=False)
    return rows


async def get_run_marketing(
    session: AsyncSession, run_id: str
) -> list[MarketingActionRecord]:
    result = await session.execute(
        select(MarketingActionRecord)
        .where(MarketingActionRecord.run_id == run_id)
        .order_by(desc(MarketingActionRecord.created_at))
    )
    return list(result.scalars().all())


async def get_run_content(
    session: AsyncSession, run_id: str
) -> list[ContentPostRecord]:
    result = await session.execute(
        select(ContentPostRecord)
        .where(ContentPostRecord.run_id == run_id)
        .order_by(desc(ContentPostRecord.is_urgent), ContentPostRecord.created_at)
    )
    return list(result.scalars().all())


async def get_run_return_insights(
    session: AsyncSession, run_id: str
) -> list[ReturnInsightRecord]:
    result = await session.execute(
        select(ReturnInsightRecord)
        .where(ReturnInsightRecord.run_id == run_id)
    )
    return list(result.scalars().all())


async def get_run_dm_replies(
    session: AsyncSession, run_id: str
) -> list[DMReplyRecord]:
    result = await session.execute(
        select(DMReplyRecord).where(DMReplyRecord.run_id == run_id)
    )
    return list(result.scalars().all())


async def get_flagged_dms(
    session: AsyncSession,
    brand_id: str,
    status: str = "flagged_open",
    limit: int = 100,
) -> list[DMReplyRecord]:
    """High priority (bulk_inquiry, complaint) sorted before normal (influencer)."""
    priority_order = {"high": 0, "normal": 1}
    result = await session.execute(
        select(DMReplyRecord)
        .where(
            DMReplyRecord.brand_id == brand_id,
            DMReplyRecord.status   == status,
        )
        .order_by(desc(DMReplyRecord.created_at))
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: priority_order.get(r.flag_priority, 9))
    return rows


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
    
def _parse_date(value: Optional[str]) -> Optional["date"]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL / UPDATE HELPERS
# brand_id included in WHERE clause — defense in depth against cross-tenant writes.
# The router already verifies ownership before calling these, but a stale or
# buggy caller can never mutate another tenant's records even if it bypasses
# the router check.
# ══════════════════════════════════════════════════════════════════════════════

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


async def get_dm_reply(
    session: AsyncSession,
    record_id: str,
) -> Optional[DMReplyRecord]:
    result = await session.execute(
        select(DMReplyRecord).where(DMReplyRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def mark_pricing_executed(
    session: AsyncSession,
    record_id: str,
    brand_id: str,
) -> Optional[PricingActionRecord]:
    """Set auto_executed=True. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(PricingActionRecord)
        .where(PricingActionRecord.id == record_id, PricingActionRecord.brand_id == brand_id)
        .values(auto_executed=True)
    )
    await session.flush()
    return await get_pricing_action(session, record_id)


async def mark_pricing_rejected(
    session: AsyncSession,
    record_id: str,
    brand_id: str,
) -> Optional[PricingActionRecord]:
    """Set action='rejected'. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(PricingActionRecord)
        .where(PricingActionRecord.id == record_id, PricingActionRecord.brand_id == brand_id)
        .values(action="rejected")
    )
    await session.flush()
    return await get_pricing_action(session, record_id)


async def mark_marketing_executed(
    session: AsyncSession,
    record_id: str,
    brand_id: str,
) -> Optional[MarketingActionRecord]:
    """Set auto_executed=True. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(MarketingActionRecord)
        .where(MarketingActionRecord.id == record_id, MarketingActionRecord.brand_id == brand_id)
        .values(auto_executed=True)
    )
    await session.flush()
    return await get_marketing_action(session, record_id)


async def mark_marketing_rejected(
    session: AsyncSession,
    record_id: str,
    brand_id: str,
) -> Optional[MarketingActionRecord]:
    """Set action='rejected'. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(MarketingActionRecord)
        .where(MarketingActionRecord.id == record_id, MarketingActionRecord.brand_id == brand_id)
        .values(action="rejected")
    )
    await session.flush()
    return await get_marketing_action(session, record_id)


async def update_restock_status(
    session: AsyncSession,
    record_id: str,
    new_status: str,   # "approved" | "ordered" | "cancelled"
    brand_id: str,
) -> Optional[RestockRecommendationRecord]:
    """Update restock status. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(RestockRecommendationRecord)
        .where(RestockRecommendationRecord.id == record_id, RestockRecommendationRecord.brand_id == brand_id)
        .values(status=new_status)
    )
    await session.flush()
    return await get_restock_recommendation(session, record_id)


async def update_content_post_status(
    session: AsyncSession,
    record_id: str,
    new_status: str,   # "posted" | "skipped"
    brand_id: str,
) -> Optional[ContentPostRecord]:
    """Update content post status. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(ContentPostRecord)
        .where(ContentPostRecord.id == record_id, ContentPostRecord.brand_id == brand_id)
        .values(status=new_status)
    )
    await session.flush()
    return await get_content_post(session, record_id)

async def mark_dm_resolved(
    session: AsyncSession,
    record_id: str,
    brand_id: str,
) -> Optional[DMReplyRecord]:
    """Set status='flagged_resolved'. brand_id in WHERE prevents cross-tenant mutation."""
    await session.execute(
        sa_update(DMReplyRecord)
        .where(DMReplyRecord.id == record_id, DMReplyRecord.brand_id == brand_id)
        .values(status="flagged_resolved")
    )
    await session.flush()
    return await get_dm_reply(session, record_id)
