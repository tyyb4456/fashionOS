"""
FashionOS — FastAPI Application
================================
Entry point for the HTTP layer. Mounts all routers and exposes:

  GET  /health          ← Docker + load balancer health check
  GET  /api/v1/status   ← Richer status (Redis, Celery worker ping)
  POST /api/v1/webhooks/shopify/{topic}    ← Shopify webhook receiver
  POST /api/v1/webhooks/manual-run         ← Manual pipeline trigger

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


# ── App metadata ──────────────────────────────────────────────────────────────

APP_VERSION = "0.1.0"
BRAND_NAME  = os.getenv("BRAND_NAME", "FashionOS Brand")
ENV         = os.getenv("ENV", "development")
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Lifespan: startup + shutdown hooks ───────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup, then yields, then runs at shutdown.
    Use for: connection pool setup, warm-up calls, graceful teardown.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    print(f"[FashionOS] Starting API — brand={BRAND_NAME} env={ENV}")

    # Verify Redis is reachable
    try:
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        print(f"[FashionOS]  Redis connected ({REDIS_URL})")
    except Exception as e:
        # Non-fatal at startup — Celery tasks will fail gracefully if Redis is down
        print(f"[FashionOS] △  Redis not reachable at startup: {e}")

    # TODO: init PostgreSQL connection pool (SQLAlchemy async engine)
    # TODO: run Alembic migrations on startup (dev only)

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    print("[FashionOS] Shutting down API.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title       = f"FashionOS API — {BRAND_NAME}",
    description = (
        "Autonomous multi-agent fashion brand operating system. "
        "This API receives Shopify webhooks, triggers agent pipelines, "
        "and exposes run status + results."
    ),
    version  = APP_VERSION,
    docs_url = "/docs" if ENV == "development" else None,   # hide swagger in prod
    lifespan = lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# In prod: replace "*" with your actual dashboard domain
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"] if ENV == "development" else [os.getenv("DASHBOARD_URL", "")],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(webhooks.router)

# TODO: add as built
# from api.routers import runs, alerts, dashboard
# app.include_router(runs.router)
# app.include_router(alerts.router)


# ── Health + status endpoints ─────────────────────────────────────────────────

@app.get("/health", tags=["ops"], summary="Docker health check")
async def health():
    """Lightweight endpoint — Docker and load balancers ping this."""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/status", tags=["ops"], summary="Richer system status")
async def status():
    """
    Checks Redis connectivity and returns system info.
    Used by the dashboard to show a live "system healthy" indicator.
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
        "status":     "ok" if redis_ok else "degraded",
        "version":    APP_VERSION,
        "brand":      BRAND_NAME,
        "env":        ENV,
        "redis":      "connected" if redis_ok else "unreachable",
        "celery":     "not checked",   # TODO: ping Celery inspect
        "agents": {
            "inventory":  "active",
            "trend":      "coming soon",
            "pricing":    "coming soon",
            "restock":    "coming soon",
            "content":    "coming soon",
            "marketing":  "coming soon",
            "dm":         "coming soon",
            "returns":    "coming soon",
        },
    }