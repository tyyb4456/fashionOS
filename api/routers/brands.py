"""
Brand management — Clerk authenticated.
GET /api/v1/brands/me       → own brand info
PUT /api/v1/brands/me       → update brand name + notification contacts only
                               (credentials come via OAuth now)

Admin:
POST /api/v1/brands/provision  → manual brand creation (X-Admin-Secret)
GET  /api/v1/brands/all        → list all brands (X-Admin-Secret)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_brand, require_admin
from db.credentials import BrandCredentials, cache_brand_credentials, decrypt_value, encrypt_value
from db.models import Brand
from db.session import get_session

router = APIRouter(prefix="/api/v1/brands", tags=["brands"])


class BrandProvisionRequest(BaseModel):
    brand_id:             str
    brand_name:           str
    owner_email:          EmailStr
    clerk_user_id:        Optional[str] = None
    plan:                 str = "starter"


class BrandUpdateRequest(BaseModel):
    """Only non-OAuth fields — credentials come through OAuth flows."""
    brand_name:           Optional[str] = None
    brand_owner_whatsapp: Optional[str] = None
    brand_owner_email:    Optional[str] = None


class BrandResponse(BaseModel):
    brand_id:             str
    brand_name:           str
    owner_email:          str
    plan:                 str
    is_active:            bool
    meta_ad_account_id:   Optional[str]
    instagram_page_id:    Optional[str]
    brand_owner_whatsapp: Optional[str]
    brand_owner_email:    Optional[str]
    shopify_connected:    bool
    meta_connected:       bool
    instagram_connected:  bool
    created_at:           datetime


def _to_response(b: Brand) -> BrandResponse:
    return BrandResponse(
        brand_id             = b.brand_id,
        brand_name           = b.brand_name,
        owner_email          = b.owner_email,
        plan                 = b.plan,
        is_active            = b.is_active,
        meta_ad_account_id   = b.meta_ad_account_id,
        instagram_page_id    = b.instagram_page_id,
        brand_owner_whatsapp = b.brand_owner_whatsapp,
        brand_owner_email    = b.brand_owner_email,
        shopify_connected    = bool(b.shopify_access_token_enc),
        meta_connected       = bool(b.meta_access_token_enc),
        instagram_connected  = bool(b.instagram_access_token_enc),
        created_at           = b.created_at,
    )


def _build_creds(b: Brand) -> BrandCredentials:
    return BrandCredentials(
        shopify_shop_name      = b.shopify_shop_name or "",
        shopify_access_token   = decrypt_value(b.shopify_access_token_enc   or ""),
        shopify_webhook_secret = decrypt_value(b.shopify_webhook_secret_enc or ""),
        meta_access_token      = decrypt_value(b.meta_access_token_enc      or ""),
        meta_ad_account_id     = b.meta_ad_account_id or "",
        instagram_access_token = decrypt_value(b.instagram_access_token_enc or ""),
        instagram_page_id      = b.instagram_page_id or "",
        brand_owner_whatsapp   = b.brand_owner_whatsapp or "",
        brand_owner_email      = b.brand_owner_email or "",
    )


# ── Admin ──────────────────────────────────────────────────────────────────────

@router.post("/provision", status_code=201)
async def provision_brand(
    req:     BrandProvisionRequest,
    _:       None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if (await session.execute(
        select(Brand).where(Brand.brand_id == req.brand_id)
    )).scalar_one_or_none():
        raise HTTPException(400, f"brand_id='{req.brand_id}' already exists.")

    brand = Brand(
        id            = uuid.uuid4(),
        brand_id      = req.brand_id,
        brand_name    = req.brand_name,
        owner_email   = req.owner_email,
        clerk_user_id = req.clerk_user_id,
        plan          = req.plan,
        is_active     = True,
    )
    session.add(brand)
    await session.flush()
    return {"brand_id": req.brand_id, "message": "Brand provisioned. Connect Shopify and Meta via OAuth."}


@router.get("/all", response_model=list[BrandResponse])
async def list_all_brands(
    _:       None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    brands = (await session.execute(select(Brand).order_by(Brand.created_at))).scalars().all()
    return [_to_response(b) for b in brands]


# ── Brand authenticated ────────────────────────────────────────────────────────

@router.get("/me", response_model=BrandResponse)
async def get_my_brand(brand: Brand = Depends(get_current_brand)):
    return _to_response(brand)


@router.put("/me", response_model=BrandResponse)
async def update_my_brand(
    req:     BrandUpdateRequest,
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
):
    """Update brand name and notification contacts. Credentials come via OAuth."""
    if req.brand_name is not None:
        brand.brand_name = req.brand_name
    if req.brand_owner_whatsapp is not None:
        brand.brand_owner_whatsapp = req.brand_owner_whatsapp
    if req.brand_owner_email is not None:
        brand.brand_owner_email = req.brand_owner_email

    brand.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await cache_brand_credentials(brand.brand_id, _build_creds(brand))
    return _to_response(brand)


@router.delete("/me/shopify", status_code=204)
async def disconnect_shopify(
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
):
    """Disconnect Shopify — clears token from DB and Redis."""
    brand.shopify_shop_name          = None
    brand.shopify_access_token_enc   = None
    brand.shopify_webhook_secret_enc = None
    brand.updated_at                 = datetime.now(timezone.utc)
    await session.flush()
    await cache_brand_credentials(brand.brand_id, _build_creds(brand))


@router.delete("/me/meta", status_code=204)
async def disconnect_meta(
    brand:   Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
):
    """Disconnect Meta — clears token from DB and Redis."""
    brand.meta_access_token_enc      = None
    brand.meta_ad_account_id         = None
    brand.instagram_access_token_enc = None
    brand.instagram_page_id          = None
    brand.updated_at                 = datetime.now(timezone.utc)
    await session.flush()
    await cache_brand_credentials(brand.brand_id, _build_creds(brand))