"""
FashionOS — FastAPI Application
================================
Entry point for the HTTP layer. Mounts all routers and exposes:

  GET  /health                                  ← Docker + load balancer health check
  GET  /api/v1/status                           ← Richer status (Redis, agent registry)
  POST /api/v1/webhooks/shopify/{topic}         ← Shopify webhook receiver
  POST /api/v1/webhooks/manual-run              ← Manual pipeline trigger
  GET  /api/v1/runs                             ← Run history list
  GET  /api/v1/runs/{run_id}                    ← Run detail with child records
  GET  /api/v1/runs/{run_id}/inventory          ← Inventory snapshots for a run
  GET  /api/v1/alerts/critical                  ← Open critical alerts
  GET  /api/v1/pricing/pending                  ← Pricing decisions awaiting approval
  GET  /api/v1/restock/pending                  ← Restock orders awaiting approval
  GET  /api/v1/skus/{sku}/history               ← Time-series inventory for a SKU
  GET  /api/v1/dashboard                        ← Aggregated dashboard summary

Start (dev):
  uvicorn api.main:app --host 0.0.0.0 --port 8080 --reload

Start (prod — via docker-compose):
  uvicorn api.main:app --host 0.0.0.0 --port 8080 --workers 2
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()   # load .env BEFORE any router modules read os.getenv()

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import webhooks
from api.routers import runs


# ── App metadata ──────────────────────────────────────────────────────────────

APP_VERSION = "0.1.0"
BRAND_NAME  = os.getenv("BRAND_NAME", "FashionOS Brand")
ENV         = os.getenv("ENV", "development")
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")


# ── Lifespan: startup + shutdown hooks ────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup, yields, then runs at shutdown.
    Use for: connection pool setup, warm-up calls, graceful teardown.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    print(f"[FashionOS] Starting API — brand={BRAND_NAME} env={ENV}")

    # Verify Redis is reachable
    try:
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        print(f"[FashionOS] ✓ Redis connected ({REDIS_URL})")
    except Exception as e:
        print(f"[FashionOS] △ Redis not reachable at startup: {e}")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    print("[FashionOS] Shutting down API.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title       = f"FashionOS API — {BRAND_NAME}",
    description = (
        "Autonomous multi-agent fashion brand operating system. "
        "Receives Shopify webhooks, triggers agent pipelines, "
        "and exposes run history, approval queues, and dashboard data."
    ),
    version  = APP_VERSION,
    docs_url = "/docs" if ENV == "development" else None,
    lifespan = lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"] if ENV == "development" else [os.getenv("DASHBOARD_URL", "")],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(webhooks.router)
app.include_router(runs.router)

# TODO: add as built
# from api.routers import alerts, dashboard
# app.include_router(alerts.router)


# ── Health + status endpoints ─────────────────────────────────────────────────

@app.get("/health", tags=["ops"], summary="Docker health check")
async def health():
    """Lightweight endpoint — Docker and load balancers ping this."""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/status", tags=["ops"], summary="System status")
async def system_status():
    """
    Checks Redis connectivity and returns system info.
    Used by the dashboard to show a live 'system healthy' indicator.
    """
    redis_ok = False
    try:
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception:
        pass

    return {
        "status":  "ok" if redis_ok else "degraded",
        "version": APP_VERSION,
        "brand":   BRAND_NAME,
        "env":     ENV,
        "redis":   "connected" if redis_ok else "unreachable",
        "agents": {
            "inventory": "active",
            "pricing":   "active",
            "restock":   "active",
            "trend":     "coming soon",
            "content":   "coming soon",
            "marketing": "coming soon",
            "dm":        "coming soon",
            "returns":   "coming soon",
        },
    }