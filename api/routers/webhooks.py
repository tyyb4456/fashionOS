"""
Shopify Webhook Router — multi-tenant.
URL per brand: POST /api/v1/webhooks/shopify/{brand_id}/{topic}
HMAC uses that brand's own webhook secret from Redis.
"""

import hashlib, hmac, json, os, base64
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_brand
from api.workers.tasks import run_agent_pipeline
from db.models import Brand
from db.session import get_session
import redis.asyncio as aioredis

router  = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


async def _get_brand(brand_id: str, session: AsyncSession) -> Brand | None:
    return (await session.execute(
        select(Brand).where(Brand.brand_id == brand_id, Brand.is_active == True)  # noqa: E712
    )).scalar_one_or_none()


async def _webhook_secret(brand_id: str) -> str:
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        raw = await r.get(f"fashionos:creds:{brand_id}")
        return json.loads(raw).get("shopify_webhook_secret", "") if raw else ""
    finally:
        await r.aclose()


def _verify(raw_body: bytes, hmac_header: str, secret: str) -> bool:
    if not secret:
        return os.getenv("ENV", "development") != "production"
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed, hmac_header)


def _sku_hint(payload: dict) -> str | None:
    try:
        return payload["line_items"][0]["sku"]
    except (KeyError, IndexError, TypeError):
        return None


@router.post("/shopify/{brand_id}/{topic_path:path}", status_code=200)
async def shopify_webhook(
    brand_id:   str,
    topic_path: str,
    request:    Request,
    session:    AsyncSession = Depends(get_session),
    x_shopify_topic:       str = Header(..., alias="X-Shopify-Topic"),
    x_shopify_hmac_sha256: str = Header(..., alias="X-Shopify-Hmac-Sha256"),
    x_shopify_shop_domain: str = Header("",  alias="X-Shopify-Shop-Domain"),
):
    brand = await _get_brand(brand_id, session)
    if not brand:
        raise HTTPException(404, f"Brand '{brand_id}' not found.")

    raw_body = await request.body()

    if not _verify(raw_body, x_shopify_hmac_sha256, await _webhook_secret(brand_id)):
        raise HTTPException(401, "Invalid webhook signature.")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON.")

    task = run_agent_pipeline.delay(
        brand_id        = brand_id,
        brand_name      = brand.brand_name,
        trigger         = "shopify_webhook",
        trigger_payload = {
            "topic":       x_shopify_topic,
            "shop_domain": x_shopify_shop_domain,
            "order_id":    payload.get("id"),
            "order_name":  payload.get("name"),
            "sku_hint":    _sku_hint(payload),
        },
    )
    return {"received": True, "brand_id": brand_id, "topic": x_shopify_topic, "task_id": task.id}


@router.post("/manual-run", status_code=202)
async def manual_run(
    agents: list[str] | None = None,
    brand:  Brand = Depends(get_current_brand),
):
    task = run_agent_pipeline.delay(
        brand_id=brand.brand_id, brand_name=brand.brand_name,
        trigger="manual", trigger_payload={}, agents_to_run=agents,
    )
    return {"accepted": True, "brand_id": brand.brand_id, "task_id": task.id}