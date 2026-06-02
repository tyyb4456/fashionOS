"""
FashionOS Celery Workers
========================
Async task queue that decouples HTTP request handling from agent execution.

Why Celery instead of asyncio directly in FastAPI?
  - Shopify webhook endpoints must return 200 within 5 seconds or Shopify retries.
  - Agent runs take 15–90 seconds (multiple Claude calls + MCP round trips).
  - Celery pushes work to Redis, FastAPI responds immediately, workers execute async.
  - Built-in retry logic, task history, and failure handling.

Task registry:
  run_agent_pipeline        ← Core task. Receives trigger + payload, runs supervisor.
  run_scheduled_inventory   ← Celery beat task. Fires every hour.
  run_scheduled_daily       ← Celery beat task. Fires at midnight PKT.

Worker startup:
  celery -A api.workers.tasks worker --loglevel=info --concurrency=4

Beat scheduler (for scheduled runs):
  celery -A api.workers.tasks beat --loglevel=info
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab
from celery.utils.log import get_task_logger

from agents.supervisor import make_initial_state, supervisor_graph


# ── Celery app setup ──────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "fashionos",
    broker=REDIS_URL,
    backend=REDIS_URL.replace("/0", "/1"),   # separate DB for results
)

celery_app.conf.update(
    # Serialization
    task_serializer   = "json",
    result_serializer = "json",
    accept_content    = ["json"],

    # Reliability
    task_acks_late            = True,   # ack AFTER task completes, not before
    task_reject_on_worker_lost = True,  # re-queue if worker dies mid-task
    worker_prefetch_multiplier = 1,     # one task at a time per worker thread
                                        # (agent tasks are heavy, don't prefetch)

    # Result expiry (keep for 24h for dashboard queries)
    result_expires = 86400,

    # Timezone
    timezone           = "Asia/Karachi",
    enable_utc         = True,

    # ── Beat schedule (periodic tasks) ───────────────────────────────────────
    beat_schedule = {
        # Hourly inventory sweep — checks stockout risk in real time
        "hourly-inventory-sweep": {
            "task":     "api.workers.tasks.run_scheduled_inventory",
            "schedule": crontab(minute=0),   # top of every hour
        },
        # Daily full sweep — inventory + (trend/pricing when built)
        "daily-full-sweep": {
            "task":     "api.workers.tasks.run_scheduled_daily",
            "schedule": crontab(hour=0, minute=5),   # 00:05 PKT daily
        },
    },
)

logger = get_task_logger(__name__)


# ── Helper: run async graph in sync Celery context ────────────────────────────

def _run_async(coro):
    """
    Celery tasks are synchronous. LangGraph graphs are async.
    This runs the coroutine in a fresh event loop — safe because each
    Celery worker process has its own isolated execution context.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# TASK 1 — run_agent_pipeline  (core task — all triggers land here)
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(
    name="api.workers.tasks.run_agent_pipeline",
    bind=True,
    max_retries=3,
    default_retry_delay=30,   # wait 30s before retry
    soft_time_limit=300,      # warn after 5 min
    time_limit=420,           # hard kill after 7 min
)
def run_agent_pipeline(
    self,
    brand_id:        str,
    brand_name:      str,
    trigger:         str,
    trigger_payload: dict,
    agents_to_run:   list | None = None,
):
    """
    Main entry point for all agent runs.

    Called by:
      - Shopify webhook handler (orders/paid, inventory_levels/update, etc.)
      - Scheduled beat tasks (run_scheduled_inventory, run_scheduled_daily)
      - Manual API endpoint (POST /api/v1/run)

    Args:
        brand_id:        Brand identifier (multi-tenancy key).
        brand_name:      Human-readable brand name (used in agent prompts).
        trigger:         "shopify_webhook" | "scheduled_run" | "manual"
        trigger_payload: Raw webhook body or schedule config dict.
        agents_to_run:   Optional explicit list for manual triggers.

    Returns:
        dict with run_id, run_summary, completed_agents, alert counts.
    """
    task_id   = self.request.id
    started   = datetime.now(timezone.utc).isoformat()

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

        # Run the full supervisor graph (sync wrapper around async graph)
        result = _run_async(supervisor_graph.ainvoke(initial_state))

        # ── Build return payload ───────────────────────────────────────────
        alerts   = result.get("alerts", [])
        critical = [a for a in alerts if a.get("level") == "critical"]
        warnings = [a for a in alerts if a.get("level") == "warning"]

        pricing      = result.get("pricing_recommendations", [])
        auto_priced  = [p for p in pricing if p.get("action") == "markdown"
                        and p.get("discount_pct", 0) <= 15]
        pending_price= [p for p in pricing if p.get("action")
                        in ("markdown", "clearance_code", "increase", "bundle")
                        and p.get("discount_pct", 0) > 15]

        summary = {
            "run_id":            result.get("run_id", initial_state["run_id"]),
            "brand_id":          brand_id,
            "brand_name":        brand_name,
            "trigger":           trigger,
            "started_at":        started,
            "completed_at":      result.get("completed_at"),
            "completed_agents":  result.get("completed_agents", []),
            "run_summary":       result.get("run_summary", ""),
            "alert_counts": {
                "critical": len(critical),
                "warning":  len(warnings),
                "total":    len(alerts),
            },
            "inventory_skus_analysed": len(result.get("inventory_snapshot", [])),
            "pricing": {
                "total_decisions":    len(pricing),
                "auto_executed":      len(auto_priced),
                "pending_approval":   len(pending_price),
            },
            "task_id": task_id,
        }

        logger.info(
            f"[{task_id}] 🗸 Pipeline complete | "
            f"agents={summary['completed_agents']} | "
            f"alerts={summary['alert_counts']}"
        )

        # TODO: persist summary to PostgreSQL (db.models.AgentRun)
        # await save_run_to_db(summary, result)

        # TODO: push critical alerts to notify-mcp (Twilio WhatsApp) when built
        # if critical:
        #     notify_task.delay(brand_id, critical)

        return summary

    except Exception as exc:
        logger.error(f"[{task_id}] ✗ Pipeline failed: {exc}", exc_info=True)

        # Retry with exponential backoff — Celery handles the delay
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


# ══════════════════════════════════════════════════════════════════════════════
# TASK 2 — Scheduled hourly inventory sweep
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(name="api.workers.tasks.run_scheduled_inventory")
def run_scheduled_inventory():
    """
    Fires every hour via Celery beat.
    Runs inventory agent only — fast, targeted stockout monitoring.

    In multi-brand SaaS mode this will loop over all active brands.
    For now it uses the single BRAND_ID from env.
    """
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
    """
    Fires daily at 00:05 PKT via Celery beat.
    Full sweep: inventory + (trend, pricing, restock once agents are built).
    """
    brand_id   = os.getenv("BRAND_ID",   "default-brand")
    brand_name = os.getenv("BRAND_NAME", "FashionOS Brand")

    logger.info(f"[beat] Daily full sweep for brand={brand_name}")

    run_agent_pipeline.delay(
        brand_id        = brand_id,
        brand_name      = brand_name,
        trigger         = "scheduled_run",
        trigger_payload = {"schedule_type": "daily"},
        # No agents_to_run — supervisor's routing table decides
    )