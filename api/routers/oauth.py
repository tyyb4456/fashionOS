"""
OAuth flows for Shopify and Meta.

Shopify:
  GET /api/v1/oauth/shopify/start?shop=mybrand  → redirect URL
  GET /api/v1/oauth/shopify/callback            → exchange code, store token, register webhooks

Meta:
  GET /api/v1/oauth/meta/start                  → redirect URL
  GET /api/v1/oauth/meta/callback               → exchange code, fetch ad account + IG page, store

Security: state = CSRF token stored in Redis (10 min TTL) → links callback to brand
"""

import hashlib
import hmac
import json
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_brand
from db.credentials import (
    BrandCredentials, cache_brand_credentials,
    decrypt_value, encrypt_value,
)
from db.models import Brand
from db.session import get_session

import redis.asyncio as aioredis

router   = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Shopify config ─────────────────────────────────────────────────────────────
SHOPIFY_CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_REDIRECT_URI  = os.getenv("SHOPIFY_REDIRECT_URI", "")
SHOPIFY_SCOPES        = os.getenv(
    "SHOPIFY_SCOPES",
    "read_products,write_products,read_orders,read_inventory,"
    "write_inventory,read_price_rules,write_price_rules",
)
SHOPIFY_API_VERSION   = "2026-04"

# ── Meta config ────────────────────────────────────────────────────────────────
META_CLIENT_ID      = os.getenv("META_CLIENT_ID", "")
META_CLIENT_SECRET  = os.getenv("META_CLIENT_SECRET", "")
META_REDIRECT_URI   = os.getenv("META_REDIRECT_URI", "")
META_SCOPES         = (
    "ads_management,ads_read,instagram_manage_messages,"
    "instagram_manage_insights,pages_read_engagement,"
    "business_management"
)
META_GRAPH_VERSION  = os.getenv("META_GRAPH_API_VERSION", "v21.0")
META_GRAPH_URL      = f"https://graph.facebook.com/{META_GRAPH_VERSION}"

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


# ── Redis helpers ──────────────────────────────────────────────────────────────

async def _set_state(state: str, brand_id: str) -> None:
    """Store brand_id against CSRF state token for 10 minutes."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.set(f"oauth:state:{state}", brand_id, ex=600)
    finally:
        await r.aclose()


async def _get_state(state: str) -> str | None:
    """Retrieve and delete brand_id for a state token."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        brand_id = await r.get(f"oauth:state:{state}")
        await r.delete(f"oauth:state:{state}")
        return brand_id
    finally:
        await r.aclose()


# ── Credential sync helper ─────────────────────────────────────────────────────

async def _sync_creds(brand: Brand) -> None:
    """Decrypt all brand credentials and push to Redis for MCP servers."""
    await cache_brand_credentials(brand.brand_id, BrandCredentials(
        shopify_shop_name      = brand.shopify_shop_name or "",
        shopify_access_token   = decrypt_value(brand.shopify_access_token_enc   or ""),
        shopify_webhook_secret = decrypt_value(brand.shopify_webhook_secret_enc or ""),
        meta_access_token      = decrypt_value(brand.meta_access_token_enc      or ""),
        meta_ad_account_id     = brand.meta_ad_account_id or "",
        instagram_access_token = decrypt_value(brand.instagram_access_token_enc or ""),
        instagram_page_id      = brand.instagram_page_id or "",
        brand_owner_whatsapp   = brand.brand_owner_whatsapp or "",
        brand_owner_email      = brand.brand_owner_email or "",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY OAUTH
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/shopify/start")
async def shopify_oauth_start(
    shop:  str   = Query(..., description="Shopify shop name without .myshopify.com"),
    brand: Brand = Depends(get_current_brand),
):
    """
    Returns Shopify OAuth URL. Frontend redirects user to this URL.
    shop: e.g. "mybrand" (not "mybrand.myshopify.com")
    """
    if not SHOPIFY_CLIENT_ID:
        raise HTTPException(500, "SHOPIFY_CLIENT_ID not configured.")

    shop_domain = shop.replace(".myshopify.com", "").strip()

    state = secrets.token_hex(16)
    await _set_state(state, brand.brand_id)

    params = urlencode({
        "client_id":    SHOPIFY_CLIENT_ID,
        "scope":        SHOPIFY_SCOPES,
        "redirect_uri": SHOPIFY_REDIRECT_URI,
        "state":        state,
    })

    return {
        "url": f"https://{shop_domain}.myshopify.com/admin/oauth/authorize?{params}",
        "shop": shop_domain,
    }


@router.get("/shopify/callback")
async def shopify_oauth_callback(
    request: Request,
    code:    str = Query(...),
    shop:    str = Query(...),
    state:   str = Query(...),
    hmac_param: str = Query("", alias="hmac"),
    session: AsyncSession = Depends(get_session),
):
    """
    Shopify redirects here after user approves.
    Exchanges code for token, stores it, registers webhooks.
    Then redirects user back to dashboard.
    """
    # ── 1. Verify HMAC (Shopify signs the callback) ────────────────────────────
    # Shopify signs ALL query params (except 'hmac'), sorted alphabetically.
    # Hardcoding only code/shop/state breaks when Shopify adds extra params like 'host'.
    if SHOPIFY_CLIENT_SECRET and hmac_param:
        params_to_sign = {
            k: v for k, v in request.query_params.items()
            if k != "hmac"
        }
        query_string = "&".join(
            f"{k}={v}" for k, v in sorted(params_to_sign.items())
        )
        computed = hmac.new(
            SHOPIFY_CLIENT_SECRET.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed, hmac_param):
            raise HTTPException(400, "Invalid HMAC signature.")

    # ── 2. Verify state → get brand_id ────────────────────────────────────────
    brand_id = await _get_state(state)
    if not brand_id:
        raise HTTPException(400, "Invalid or expired state. Please try connecting again.")

    brand = (await session.execute(
        select(Brand).where(Brand.brand_id == brand_id)
    )).scalar_one_or_none()
    if not brand:
        raise HTTPException(404, "Brand not found.")

    shop_domain = shop.replace(".myshopify.com", "")

    # ── 3. Exchange code for permanent access token ────────────────────────────
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://{shop_domain}.myshopify.com/admin/oauth/access_token",
            json={
                "client_id":     SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "code":          code,
            },
        )
        if not r.is_success:
            raise HTTPException(502, f"Shopify token exchange failed: {r.text}")
        data = r.json()

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(502, "No access_token in Shopify response.")

    # ── 4. Generate webhook secret ─────────────────────────────────────────────
    webhook_secret = secrets.token_hex(32)

    # ── 5. Store in DB (encrypted) ─────────────────────────────────────────────
    brand.shopify_shop_name          = shop_domain
    brand.shopify_access_token_enc   = encrypt_value(access_token)
    brand.shopify_webhook_secret_enc = encrypt_value(webhook_secret)
    await session.flush()
    await _sync_creds(brand)

    # ── 6. Register webhooks automatically ─────────────────────────────────────
    topics = [
        "orders/paid",
        "orders/cancelled",
        "refunds/create",
        "inventory_levels/update",
        "products/update",
    ]
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    base = f"https://{shop_domain}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        for topic in topics:
            webhook_url = f"{os.getenv('API_BASE_URL', 'https://your-domain.com')}/api/v1/webhooks/shopify/{brand_id}/{topic}"
            await client.post(
                f"{base}/webhooks.json",
                headers=headers,
                json={
                    "webhook": {
                        "topic":   topic,
                        "address": webhook_url,
                        "format":  "json",
                    }
                },
            )

    print(f"[OAuth] ✓ Shopify connected for brand={brand_id} shop={shop_domain}, {len(topics)} webhooks registered.")

    return RedirectResponse(url=f"{FRONTEND_URL}/settings?shopify=connected")


# ══════════════════════════════════════════════════════════════════════════════
# META OAUTH
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/meta/start")
async def meta_oauth_start(brand: Brand = Depends(get_current_brand)):
    """Returns Meta (Facebook) OAuth URL. Frontend redirects user to this URL."""
    if not META_CLIENT_ID:
        raise HTTPException(500, "META_CLIENT_ID not configured.")

    state = secrets.token_hex(16)
    await _set_state(state, brand.brand_id)

    params = urlencode({
        "client_id":     META_CLIENT_ID,
        "redirect_uri":  META_REDIRECT_URI,
        "scope":         META_SCOPES,
        "state":         state,
        "response_type": "code",
    })

    return {"url": f"https://www.facebook.com/{META_GRAPH_VERSION}/dialog/oauth?{params}"}


@router.get("/meta/callback")
async def meta_oauth_callback(
    code:          str | None = Query(None),
    state:         str | None = Query(None),
    error_code:    str | None = Query(None, alias="error_code"),
    error_message: str | None = Query(None, alias="error_message"),
    session: AsyncSession = Depends(get_session),
):
    """
    Meta redirects here after user approves.
    Exchanges code for long-lived token, auto-fetches ad account + IG page.
    Meta sends error_code + error_message (instead of code + state) on failure.
    """
    # ── 0. Handle Meta OAuth errors (e.g. rejected scope, user cancelled) ─────
    if error_code:
        raise HTTPException(
            400,
            f"Meta OAuth failed (error {error_code}): {error_message or 'unknown error'}. "
            "Check your Meta App permissions in the developer dashboard."
        )
    if not code or not state:
        raise HTTPException(400, "Missing code or state — invalid callback.")

    # ── 1. Verify state ────────────────────────────────────────────────────────
    brand_id = await _get_state(state)
    if not brand_id:
        raise HTTPException(400, "Invalid or expired state. Please try connecting again.")

    brand = (await session.execute(
        select(Brand).where(Brand.brand_id == brand_id)
    )).scalar_one_or_none()
    if not brand:
        raise HTTPException(404, "Brand not found.")

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── 2. Exchange code for short-lived token ─────────────────────────────
        r = await client.get(f"{META_GRAPH_URL}/oauth/access_token", params={
            "client_id":     META_CLIENT_ID,
            "client_secret": META_CLIENT_SECRET,
            "redirect_uri":  META_REDIRECT_URI,
            "code":          code,
        })
        if not r.is_success:
            raise HTTPException(502, f"Meta token exchange failed: {r.text}")
        short_token = r.json().get("access_token")

        # ── 3. Exchange for long-lived token (60 days) ─────────────────────────
        r = await client.get(f"{META_GRAPH_URL}/oauth/access_token", params={
            "grant_type":        "fb_exchange_token",
            "client_id":         META_CLIENT_ID,
            "client_secret":     META_CLIENT_SECRET,
            "fb_exchange_token": short_token,
        })
        long_token = r.json().get("access_token", short_token)

        # ── 4. Auto-fetch Ad Account ───────────────────────────────────────────
        r = await client.get(f"{META_GRAPH_URL}/me/adaccounts", params={
            "access_token": long_token,
            "fields":       "id,name,account_status",
        })
        ad_accounts = r.json().get("data", [])
        # pick first active account (status 1 = active)
        ad_account_id = None
        for acc in ad_accounts:
            if acc.get("account_status") == 1:
                ad_account_id = acc["id"]  # already has act_ prefix
                break
        if not ad_account_id and ad_accounts:
            ad_account_id = ad_accounts[0]["id"]

        # ── 5. Auto-fetch Instagram Page ───────────────────────────────────────
        r = await client.get(f"{META_GRAPH_URL}/me/accounts", params={
            "access_token": long_token,
            "fields":       "id,name,instagram_business_account",
        })
        pages          = r.json().get("data", [])
        instagram_page_id      = None
        instagram_access_token = long_token  # fallback

        for page in pages:
            ig = page.get("instagram_business_account")
            if ig:
                instagram_page_id = ig.get("id")
                # Get page-specific token for Instagram messaging
                r2 = await client.get(f"{META_GRAPH_URL}/{page['id']}", params={
                    "access_token": long_token,
                    "fields":       "access_token",
                })
                instagram_access_token = r2.json().get("access_token", long_token)
                break

    # ── 6. Store in DB (encrypted) ─────────────────────────────────────────────
    brand.meta_access_token_enc      = encrypt_value(long_token)
    brand.meta_ad_account_id         = ad_account_id or ""
    brand.instagram_access_token_enc = encrypt_value(instagram_access_token)
    brand.instagram_page_id          = instagram_page_id or ""
    await session.flush()
    await _sync_creds(brand)

    print(
        f"[OAuth] ✓ Meta connected for brand={brand_id} "
        f"ad_account={ad_account_id} ig_page={instagram_page_id}"
    )

    return RedirectResponse(url=f"{FRONTEND_URL}/settings?meta=connected")