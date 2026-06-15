"""
FashionOS Celery Workers
========================
Session 7: Added run_scheduled_dm task + dm-check beat entry (every 30 min).

Beat schedule:
  hourly-inventory-sweep  → every hour, :00
  daily-full-sweep        → daily at 00:05 PKT
  dm-check                → every 30 min  ← NEW session 7
"""

import asyncio
import os
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab
from celery.utils.log import get_task_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agents.supervisor import make_initial_state, supervisor_graph
from db import crud


# ── Celery app ────────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "fashionos",
    broker=REDIS_URL,
    backend=REDIS_URL.replace("/0", "/1"),
)

celery_app.conf.update(
    task_serializer   = "json",
    result_serializer = "json",
    accept_content    = ["json"],

    task_acks_late             = True,
    task_reject_on_worker_lost = True,
    worker_prefetch_multiplier = 1,

    result_expires = 86400,
    timezone       = "Asia/Karachi",
    enable_utc     = True,

    beat_schedule = {
        "hourly-inventory-sweep": {
            "task":     "api.workers.tasks.run_scheduled_inventory",
            "schedule": crontab(minute=0),
        },
        "daily-full-sweep": {
            "task":     "api.workers.tasks.run_scheduled_daily",
            "schedule": crontab(hour=0, minute=5),
        },
        # NEW session 7 — poll DMs every 30 minutes
        "dm-check": {
            "task":     "api.workers.tasks.run_scheduled_dm",
            "schedule": crontab(minute="*/30"),
        },
    },
)

logger = get_task_logger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

import asyncio

# --- Persistent event loop (lives for the entire worker process) ---
_worker_loop: asyncio.AbstractEventLoop | None = None

def _get_worker_loop() -> asyncio.AbstractEventLoop:
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop

def _run_async(coro):
    return _get_worker_loop().run_until_complete(coro)

async def _save_run_to_db(summary: dict, result: dict, task_id: str) -> None:
    engine  = create_async_engine(DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as session:
            async with session.begin():
                await crud.save_run(session, summary, result)
        logger.info(f"[{task_id}] DB: Run persisted (run_id={summary['run_id']}).")
    except Exception as db_exc:
        logger.error(f"[{task_id}] DB: Persist failed (non-fatal) — {db_exc}", exc_info=True)
    finally:
        await engine.dispose()


# ══════════════════════════════════════════════════════════════════════════════
# TASK 1 — run_agent_pipeline
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(
    name="api.workers.tasks.run_agent_pipeline",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=300,
    time_limit=420,
)
def run_agent_pipeline(
    self,
    brand_id:        str,
    brand_name:      str,
    trigger:         str,
    trigger_payload: dict,
    agents_to_run:   list | None = None,
):
    task_id = self.request.id
    started = datetime.now(timezone.utc).isoformat()

    logger.info(
        f"[{task_id}] Starting pipeline | brand={brand_name} | "
        f"trigger={trigger} | topic={trigger_payload.get('topic', 'n/a')}"
    )

    try:
        initial_state = make_initial_state(
            brand_id        = brand_id,
            brand_name      = brand_name,
            trigger         = trigger,
            trigger_payload = trigger_payload,
            agents_to_run   = agents_to_run,
        )

        async def _run_pipeline_and_save():
            result = await supervisor_graph.ainvoke(initial_state)

            alerts        = result.get("alerts", [])
            critical      = [a for a in alerts if a.get("level") == "critical"]
            warnings      = [a for a in alerts if a.get("level") == "warning"]

            pricing       = result.get("pricing_recommendations", [])
            auto_priced   = [p for p in pricing if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15]
            pending_price = [p for p in pricing if p.get("action") in ("markdown", "clearance_code", "increase", "bundle") and p.get("discount_pct", 0) > 15]

            marketing         = result.get("marketing_actions", [])
            auto_marketing    = [m for m in marketing if m.get("auto_executed")]
            pending_marketing = [m for m in marketing if not m.get("auto_executed") and m.get("action") not in ("hold",)]

            # DM stats (NEW session 7)
            dm_replies    = result.get("dm_replies", [])
            dm_auto_sent  = [r for r in dm_replies if r.get("auto_sent")]
            dm_flagged    = [r for r in dm_replies if r.get("flagged")]

            summary = {
                "run_id":           result.get("run_id", initial_state["run_id"]),
                "brand_id":         brand_id,
                "brand_name":       brand_name,
                "trigger":          trigger,
                "started_at":       started,
                "completed_at":     result.get("completed_at"),
                "completed_agents": result.get("completed_agents", []),
                "run_summary":      result.get("run_summary", ""),
                "alert_counts": {
                    "critical": len(critical),
                    "warning":  len(warnings),
                    "total":    len(alerts),
                },
                "inventory_skus_analysed": len(result.get("inventory_snapshot", [])),
                "pricing": {
                    "total_decisions":  len(pricing),
                    "auto_executed":    len(auto_priced),
                    "pending_approval": len(pending_price),
                },
                "marketing": {
                    "total_decisions":  len(marketing),
                    "auto_executed":    len(auto_marketing),
                    "pending_approval": len(pending_marketing),
                },
                # DM stats — not persisted to DB yet (session 8)
                "dm": {
                    "auto_replied": len(dm_auto_sent),
                    "flagged":      len(dm_flagged),
                },
                "task_id": task_id,
            }

            await _save_run_to_db(summary, result, task_id)
            return result, summary

        result, summary = _run_async(_run_pipeline_and_save())

        logger.info(
            f"[{task_id}] ✓ Pipeline complete | "
            f"agents={summary['completed_agents']} | "
            f"alerts={summary['alert_counts']} | "
            f"dm={summary['dm']}"
        )
        return summary

    except Exception as exc:
        logger.error(f"[{task_id}] ✗ Pipeline failed: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


async def _fetch_active_brands() -> list[tuple[str, str]]:
    """Returns list of (brand_id, brand_name) for all active brands."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from db.models import Brand

    engine  = create_async_engine(DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as session:
            result = await session.execute(
                select(Brand.brand_id, Brand.brand_name).where(Brand.is_active == True)  # noqa: E712
            )
            return result.all()
    finally:
        await engine.dispose()

# ══════════════════════════════════════════════════════════════════════════════
# TASK 2 — Hourly inventory sweep (all active brands)
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_inventory")
def run_scheduled_inventory():
    brands = _run_async(_fetch_active_brands())
    logger.info(f"[beat] Hourly sweep — {len(brands)} active brands.")
    for brand_id, brand_name in brands:
        run_agent_pipeline.delay(
            brand_id=brand_id, brand_name=brand_name,
            trigger="scheduled_run",
            trigger_payload={"schedule_type": "hourly"},
            agents_to_run=["inventory"],
        )

# ══════════════════════════════════════════════════════════════════════════════
# TASK 3 — Daily full sweep (all active brands)
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_daily")
def run_scheduled_daily():
    brands = _run_async(_fetch_active_brands())
    logger.info(f"[beat] Daily sweep — {len(brands)} active brands.")
    for i, (brand_id, brand_name) in enumerate(brands):
        run_agent_pipeline.apply_async(
            kwargs=dict(
                brand_id=brand_id, brand_name=brand_name,
                trigger="scheduled_run",
                trigger_payload={"schedule_type": "daily"},
            ),
            countdown=i * 30,   # stagger 30s per brand — avoids thundering herd
        )

# ══════════════════════════════════════════════════════════════════════════════
# TASK 4 — DM check (every 30 min)  — NEW session 7
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_dm")
def run_scheduled_dm():
    brands = _run_async(_fetch_active_brands())
    for brand_id, brand_name in brands:
        run_agent_pipeline.delay(
            brand_id=brand_id, brand_name=brand_name,
            trigger="scheduled_run",
            trigger_payload={"schedule_type": "dm"},
            agents_to_run=["dm"],
        )