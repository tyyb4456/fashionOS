"""
FashionOS — Shopify Webhook Router
===================================
Receives Shopify webhook POSTs, verifies their HMAC signature,
then dispatches an async Celery task. Returns 200 in < 200ms.

Registered endpoints:
  POST /api/v1/webhooks/shopify/{topic}

Shopify sends webhooks for:
  orders/paid               → inventory agent run
  orders/cancelled          → inventory agent run (stock restored)
  inventory_levels/update   → inventory agent run
  products/create           → inventory agent run
  products/update           → inventory agent run

Setup (in Shopify admin or via shopify-mcp in the future):
  1. Go to Admin → Notifications → Webhooks
  2. Add webhook for each topic above
  3. URL: https://your-domain.com/api/v1/webhooks/shopify/orders_paid
  4. Secret: must match SHOPIFY_WEBHOOK_SECRET in .env
"""

import hashlib
import hmac
import json
import os
import base64

from fastapi import APIRouter, Header, HTTPException, Request, status

from api.workers.tasks import run_agent_pipeline


router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
BRAND_ID               = os.getenv("BRAND_ID",               "default-brand")
BRAND_NAME             = os.getenv("BRAND_NAME",             "FashionOS Brand")


# ── HMAC verification ─────────────────────────────────────────────────────────

def _verify_shopify_hmac(raw_body: bytes, hmac_header: str) -> bool:
    """
    Verifies the X-Shopify-Hmac-Sha256 header against the raw request body.

    Shopify signs every webhook with HMAC-SHA256 using your webhook secret.
    We must verify this BEFORE touching the payload — it's the only guarantee
    the request is actually from Shopify and not a spoofed attacker.

    Returns True if valid, False if not.
    Deliberately returns bool (not raises) so the caller controls the 401.
    """
    if not SHOPIFY_WEBHOOK_SECRET:
        # No secret configured — skip verification in dev, fail in prod
        if os.getenv("ENV", "development") == "production":
            return False
        return True   # dev: pass through

    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()

    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@router.post(
    "/shopify/{topic_path:path}",
    status_code=status.HTTP_200_OK,
    summary="Receive Shopify webhook",
    description=(
        "Shopify sends a POST here whenever a subscribed event fires. "
        "We verify the HMAC signature, then push the event to Celery. "
        "Must return 200 within 5 seconds or Shopify will retry."
    ),
)
async def shopify_webhook(
    topic_path: str,
    request:    Request,
    x_shopify_topic:       str = Header(..., alias="X-Shopify-Topic"),
    x_shopify_hmac_sha256: str = Header(..., alias="X-Shopify-Hmac-Sha256"),
    x_shopify_shop_domain: str = Header("", alias="X-Shopify-Shop-Domain"),
):
    """
    topic_path examples:
      orders/paid           (from URL path)
    x_shopify_topic:
      orders/paid           (from Shopify header — should match path)

    We use the header as the canonical topic (Shopify guarantees it).
    """
    # ── 1. Read raw body (needed for HMAC verification before parsing) ────────
    raw_body = await request.body()

    # ── 2. Verify HMAC ────────────────────────────────────────────────────────
    if not _verify_shopify_hmac(raw_body, x_shopify_hmac_sha256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature. Request rejected.",
        )

    # ── 3. Parse JSON payload ─────────────────────────────────────────────────
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body is not valid JSON.",
        )

    topic = x_shopify_topic   # e.g. "orders/paid"

    # ── 4. Dispatch Celery task (non-blocking) ────────────────────────────────
    task = run_agent_pipeline.delay(
        brand_id        = BRAND_ID,
        brand_name      = BRAND_NAME,
        trigger         = "shopify_webhook",
        trigger_payload = {
            "topic":       topic,
            "shop_domain": x_shopify_shop_domain,
            # Include key IDs for traceability (not the full payload — too large)
            "order_id":    payload.get("id"),
            "order_name":  payload.get("name"),          # e.g. "#1042"
            "sku_hint":    _extract_sku_hint(payload),   # first SKU if present
        },
    )

    return {
        "received":  True,
        "topic":     topic,
        "task_id":   task.id,
        "message":   f"Webhook accepted. Agent pipeline dispatched (task={task.id}).",
    }


def _extract_sku_hint(payload: dict) -> str | None:
    """
    Pull the first SKU from a Shopify order payload for logging/tracing.
    Non-critical — returns None if structure doesn't match.
    """
    try:
        return payload["line_items"][0]["sku"]
    except (KeyError, IndexError, TypeError):
        return None


# ── Manual trigger endpoint ───────────────────────────────────────────────────

@router.post(
    "/manual-run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Manually trigger an agent pipeline run",
    description=(
        "Triggers the full supervisor pipeline manually. "
        "Useful for testing, dashboard 'Run now' buttons, and one-off sweeps."
    ),
)
async def manual_run(
    agents: list[str] | None = None,
):
    """
    agents: Optional explicit list of agents to run.
            e.g. ["inventory"]
            If omitted, supervisor routing table decides.
    """
    task = run_agent_pipeline.delay(
        brand_id        = BRAND_ID,
        brand_name      = BRAND_NAME,
        trigger         = "manual",
        trigger_payload = {},
        agents_to_run   = agents,
    )

    return {
        "accepted":     True,
        "task_id":      task.id,
        "agents":       agents or "supervisor will decide",
        "message":      f"Manual run dispatched (task={task.id}).",
    }