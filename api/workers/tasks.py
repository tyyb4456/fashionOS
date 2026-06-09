"""
FashionOS Celery Workers
========================
Async task queue that decouples HTTP request handling from agent execution.

Worker startup (Windows dev):
  celery -A api.workers.tasks worker --loglevel=info --pool=solo

Beat scheduler:
  celery -A api.workers.tasks beat --loglevel=info

Session 6 change:
  Summary dict now includes "marketing" stats block so the new
  marketing_decisions_total / marketing_auto_executed / marketing_pending_approval
  cached columns on agent_runs are populated correctly.

DB + asyncpg + Windows design note:
  _save_run_to_db() creates a fresh NullPool engine inside the running event loop.
  Never re-raise DB exceptions — a DB hiccup must not retry the entire agent pipeline.
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
    },
)

logger = get_task_logger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fashionos:fashionos_dev@localhost:5432/fashionos",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_async(coro):
    """Run one coroutine in a fresh event loop. Call exactly once per task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _save_run_to_db(summary: dict, result: dict, task_id: str) -> None:
    """
    Persists a completed run using a fresh NullPool engine.
    NullPool avoids the Windows ProactorEventLoop stale-connection issue.
    Never raises — a DB failure must not retry the agent pipeline.
    """
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as session:
            async with session.begin():
                await crud.save_run(session, summary, result)
        logger.info(f"[{task_id}] DB: Run persisted (run_id={summary['run_id']}).")
    except Exception as db_exc:
        logger.error(
            f"[{task_id}] DB: Persist failed (non-fatal) — {db_exc}",
            exc_info=True,
        )
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
            # ── Step 1: run the supervisor graph ──────────────────────────────
            result = await supervisor_graph.ainvoke(initial_state)

            # ── Step 2: build summary ─────────────────────────────────────────
            alerts        = result.get("alerts", [])
            critical      = [a for a in alerts if a.get("level") == "critical"]
            warnings      = [a for a in alerts if a.get("level") == "warning"]

            pricing       = result.get("pricing_recommendations", [])
            auto_priced   = [
                p for p in pricing
                if p.get("action") == "markdown" and p.get("discount_pct", 0) <= 15
            ]
            pending_price = [
                p for p in pricing
                if p.get("action") in ("markdown", "clearance_code", "increase", "bundle")
                and p.get("discount_pct", 0) > 15
            ]

            # Marketing stats (NEW session 6)
            marketing     = result.get("marketing_actions", [])
            auto_marketing  = [m for m in marketing if m.get("auto_executed")]
            pending_marketing = [
                m for m in marketing
                if not m.get("auto_executed") and m.get("action") not in ("hold",)
            ]

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
                # Marketing (NEW session 6)
                "marketing": {
                    "total_decisions":  len(marketing),
                    "auto_executed":    len(auto_marketing),
                    "pending_approval": len(pending_marketing),
                },
                "task_id": task_id,
            }

            # ── Step 3: persist with a fresh NullPool engine ──────────────────
            await _save_run_to_db(summary, result, task_id)

            return result, summary

        result, summary = _run_async(_run_pipeline_and_save())

        logger.info(
            f"[{task_id}] ✓ Pipeline complete | "
            f"agents={summary['completed_agents']} | "
            f"alerts={summary['alert_counts']} | "
            f"marketing={summary['marketing']}"
        )

        return summary

    except Exception as exc:
        logger.error(f"[{task_id}] ✗ Pipeline failed: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


# ══════════════════════════════════════════════════════════════════════════════
# TASK 2 — Hourly inventory sweep
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_inventory")
def run_scheduled_inventory():
    brand_id   = os.getenv("BRAND_ID",   "default-brand")
    brand_name = os.getenv("BRAND_NAME", "FashionOS Brand")
    logger.info(f"[beat] Hourly inventory sweep for brand={brand_name}")
    run_agent_pipeline.delay(
        brand_id        = brand_id,
        brand_name      = brand_name,
        trigger         = "scheduled_run",
        trigger_payload = {"schedule_type": "hourly"},
        agents_to_run   = ["inventory"],
    )


# ══════════════════════════════════════════════════════════════════════════════
# TASK 3 — Daily full sweep
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_daily")
def run_scheduled_daily():
    brand_id   = os.getenv("BRAND_ID",   "default-brand")
    brand_name = os.getenv("BRAND_NAME", "FashionOS Brand")
    logger.info(f"[beat] Daily full sweep for brand={brand_name}")
    run_agent_pipeline.delay(
        brand_id        = brand_id,
        brand_name      = brand_name,
        trigger         = "scheduled_run",
        trigger_payload = {"schedule_type": "daily"},
    )