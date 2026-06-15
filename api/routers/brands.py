"""
Brand management router.
Admin:  POST /api/v1/brands/provision   (X-Admin-Secret)
        GET  /api/v1/brands/all         (X-Admin-Secret)
Brand:  GET  /api/v1/brands/me          (X-API-Key)
        PUT  /api/v1/brands/me          (X-API-Key)
        POST/GET/DELETE /api/v1/brands/me/api-keys
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import generate_api_key, get_current_brand, require_admin
from db.credentials import (
    BrandCredentials, cache_brand_credentials,
    decrypt_value, encrypt_value,
)
from db.models import ApiKey, Brand
from db.session import get_session

router = APIRouter(prefix="/api/v1/brands", tags=["brands"])


class BrandProvisionRequest(BaseModel):
    brand_id:                   str
    brand_name:                 str
    owner_email:                EmailStr
    plan:                       str = "starter"
    shopify_shop_name:          Optional[str] = None
    shopify_access_token:       Optional[str] = None
    shopify_webhook_secret:     Optional[str] = None
    meta_access_token:          Optional[str] = None
    meta_ad_account_id:         Optional[str] = None
    instagram_access_token:     Optional[str] = None
    instagram_page_id:          Optional[str] = None
    brand_owner_whatsapp:       Optional[str] = None
    brand_owner_email:          Optional[str] = None


class BrandUpdateRequest(BaseModel):
    brand_name:                 Optional[str] = None
    shopify_shop_name:          Optional[str] = None
    shopify_access_token:       Optional[str] = None
    shopify_webhook_secret:     Optional[str] = None
    meta_access_token:          Optional[str] = None
    meta_ad_account_id:         Optional[str] = None
    instagram_access_token:     Optional[str] = None
    instagram_page_id:          Optional[str] = None
    brand_owner_whatsapp:       Optional[str] = None
    brand_owner_email:          Optional[str] = None


class BrandResponse(BaseModel):
    brand_id:             str
    brand_name:           str
    owner_email:          str
    plan:                 str
    is_active:            bool
    shopify_shop_name:    Optional[str]
    meta_ad_account_id:   Optional[str]
    instagram_page_id:    Optional[str]
    brand_owner_whatsapp: Optional[str]
    brand_owner_email:    Optional[str]
    has_shopify_token:    bool
    has_meta_token:       bool
    has_instagram_token:  bool
    created_at:           datetime


class ApiKeyResponse(BaseModel):
    id:           str
    brand_id:     str
    key_prefix:   str
    label:        Optional[str]
    is_active:    bool
    created_at:   datetime
    last_used_at: Optional[datetime]


class ApiKeyCreateResponse(ApiKeyResponse):
    key: str  # shown ONCE


def _to_response(b: Brand) -> BrandResponse:
    return BrandResponse(
        brand_id             = b.brand_id,
        brand_name           = b.brand_name,
        owner_email          = b.owner_email,
        plan                 = b.plan,
        is_active            = b.is_active,
        shopify_shop_name    = b.shopify_shop_name,
        meta_ad_account_id   = b.meta_ad_account_id,
        instagram_page_id    = b.instagram_page_id,
        brand_owner_whatsapp = b.brand_owner_whatsapp,
        brand_owner_email    = b.brand_owner_email,
        has_shopify_token    = bool(b.shopify_access_token_enc),
        has_meta_token       = bool(b.meta_access_token_enc),
        has_instagram_token  = bool(b.instagram_access_token_enc),
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


@router.post("/provision", status_code=201)
async def provision_brand(
    req: BrandProvisionRequest,
    _:   None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if (await session.execute(select(Brand).where(Brand.brand_id == req.brand_id))).scalar_one_or_none():
        raise HTTPException(400, f"brand_id='{req.brand_id}' already exists.")

    brand = Brand(
        id                          = uuid.uuid4(),
        brand_id                    = req.brand_id,
        brand_name                  = req.brand_name,
        owner_email                 = req.owner_email,
        plan                        = req.plan,
        is_active                   = True,
        shopify_shop_name           = req.shopify_shop_name,
        shopify_access_token_enc    = encrypt_value(req.shopify_access_token   or ""),
        shopify_webhook_secret_enc  = encrypt_value(req.shopify_webhook_secret or ""),
        meta_access_token_enc       = encrypt_value(req.meta_access_token      or ""),
        meta_ad_account_id          = req.meta_ad_account_id,
        instagram_access_token_enc  = encrypt_value(req.instagram_access_token or ""),
        instagram_page_id           = req.instagram_page_id,
        brand_owner_whatsapp        = req.brand_owner_whatsapp,
        brand_owner_email           = req.brand_owner_email,
    )
    session.add(brand)

    full_key, prefix, key_hash = generate_api_key(req.brand_id)
    session.add(ApiKey(
        id=uuid.uuid4(), brand_id=req.brand_id,
        key_prefix=prefix, key_hash=key_hash, label="default", is_active=True,
    ))
    await session.flush()
    await cache_brand_credentials(req.brand_id, _build_creds(brand))

    return {"brand_id": req.brand_id, "api_key": full_key, "message": "Save api_key now — never shown again."}


@router.get("/all", response_model=list[BrandResponse])
async def list_all_brands(_: None = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    brands = (await session.execute(select(Brand).order_by(Brand.created_at))).scalars().all()
    return [_to_response(b) for b in brands]


@router.get("/me", response_model=BrandResponse)
async def get_my_brand(brand: Brand = Depends(get_current_brand)):
    return _to_response(brand)


@router.put("/me", response_model=BrandResponse)
async def update_my_brand(
    req: BrandUpdateRequest,
    brand: Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
):
    # (field_on_request, model_field, encrypt?)
    fields = [
        ("brand_name",             "brand_name",                False),
        ("shopify_shop_name",      "shopify_shop_name",         False),
        ("shopify_access_token",   "shopify_access_token_enc",  True),
        ("shopify_webhook_secret", "shopify_webhook_secret_enc",True),
        ("meta_access_token",      "meta_access_token_enc",     True),
        ("meta_ad_account_id",     "meta_ad_account_id",        False),
        ("instagram_access_token", "instagram_access_token_enc",True),
        ("instagram_page_id",      "instagram_page_id",         False),
        ("brand_owner_whatsapp",   "brand_owner_whatsapp",      False),
        ("brand_owner_email",      "brand_owner_email",         False),
    ]
    for req_field, model_field, should_encrypt in fields:
        val = getattr(req, req_field, None)
        if val is not None:
            setattr(brand, model_field, encrypt_value(val) if should_encrypt else val)

    brand.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await cache_brand_credentials(brand.brand_id, _build_creds(brand))
    return _to_response(brand)


@router.post("/me/api-keys", status_code=201, response_model=ApiKeyCreateResponse)
async def create_api_key(
    label: Optional[str] = None,
    brand: Brand = Depends(get_current_brand),
    session: AsyncSession = Depends(get_session),
):
    full_key, prefix, key_hash = generate_api_key(brand.brand_id)
    rec = ApiKey(id=uuid.uuid4(), brand_id=brand.brand_id, key_prefix=prefix, key_hash=key_hash, label=label, is_active=True)
    session.add(rec)
    await session.flush()
    return ApiKeyCreateResponse(
        id=str(rec.id), brand_id=rec.brand_id, key_prefix=rec.key_prefix,
        label=rec.label, is_active=rec.is_active, created_at=rec.created_at,
        last_used_at=None, key=full_key,
    )


@router.get("/me/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(brand: Brand = Depends(get_current_brand), session: AsyncSession = Depends(get_session)):
    keys = (await session.execute(select(ApiKey).where(ApiKey.brand_id == brand.brand_id))).scalars().all()
    return [ApiKeyResponse(id=str(k.id), brand_id=k.brand_id, key_prefix=k.key_prefix, label=k.label,
                           is_active=k.is_active, created_at=k.created_at, last_used_at=k.last_used_at) for k in keys]


@router.delete("/me/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, brand: Brand = Depends(get_current_brand), session: AsyncSession = Depends(get_session)):
    rec = (await session.execute(
        select(ApiKey).where(ApiKey.id == uuid.UUID(key_id), ApiKey.brand_id == brand.brand_id)
    )).scalar_one_or_none()
    if not rec:
        raise HTTPException(404, "API key not found.")
    rec.is_active = False

@router.post("/admin/reset-key/{brand_id}", status_code=201)
async def admin_reset_api_key(
    brand_id: str,
    label:    Optional[str] = None,
    _:        None = Depends(require_admin),
    session:  AsyncSession = Depends(get_session),
):
    """Admin-only: generate a new API key for any brand. Use when key is lost."""
    brand = (await session.execute(
        select(Brand).where(Brand.brand_id == brand_id)
    )).scalar_one_or_none()

    if not brand:
        raise HTTPException(404, f"Brand '{brand_id}' not found.")

    full_key, prefix, key_hash = generate_api_key(brand_id)
    session.add(ApiKey(
        id         = uuid.uuid4(),
        brand_id   = brand_id,
        key_prefix = prefix,
        key_hash   = key_hash,
        label      = label or "reset",
        is_active  = True,
    ))
    await session.flush()

    return {
        "brand_id": brand_id,
        "api_key":  full_key,   # save this now
        "message":  "New key generated. Save it — never shown again.",
    }