"""
FashionOS — FastAPI Application
================================
Session 8: Approvals router mounted. notify-mcp startup check added.
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import webhooks
from api.routers import runs
from api.routers import webhooks, runs, approvals, brands, clerk_webhooks, oauth

APP_VERSION = "0.2.0"
BRAND_NAME  = os.getenv("BRAND_NAME", "FashionOS Brand")
ENV         = os.getenv("ENV", "development")
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
NOTIFY_MCP_URL = os.getenv("NOTIFY_MCP_URL", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[FashionOS] Starting API v{APP_VERSION} — brand={BRAND_NAME} env={ENV}")

    # Redis check
    try:
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        print(f"[FashionOS] ✓ Redis connected")
    except Exception as e:
        print(f"[FashionOS] △ Redis not reachable: {e}")

    # notify-mcp check (non-blocking — just informational)
    if NOTIFY_MCP_URL:
        print(f"[FashionOS] ✓ notify-mcp configured ({NOTIFY_MCP_URL})")
    else:
        print("[FashionOS] △ NOTIFY_MCP_URL not set — WhatsApp/email notifications disabled")

    yield
    print("[FashionOS] Shutting down API.")


app = FastAPI(
    title       = f"FashionOS API — {BRAND_NAME}",
    description = (
        "Autonomous multi-agent fashion brand OS. "
        "Receives Shopify webhooks, triggers agent pipelines, "
        "exposes run history, approval queues, and dashboard data."
    ),
    version  = APP_VERSION,
    docs_url = "/docs" if ENV == "development" else None,
    lifespan = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"] if ENV == "development" else [os.getenv("DASHBOARD_URL", "")],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

app.include_router(webhooks.router)
app.include_router(runs.router)
app.include_router(approvals.router)   
app.include_router(brands.router) 
app.include_router(clerk_webhooks.router)
app.include_router(oauth.router)      


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/status", tags=["ops"])
async def system_status():
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
        "notifications": "enabled" if NOTIFY_MCP_URL else "disabled",
        "agents": {
            "inventory": "active", "trend":     "active",
            "pricing":   "active", "restock":   "active",
            "content":   "active", "returns":   "active",
            "marketing": "active", "dm":        "active",
        },
    }