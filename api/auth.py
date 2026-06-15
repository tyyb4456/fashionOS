"""
FashionOS API Key Authentication
==================================
API key format:  fos_{brand_prefix}_{48_hex_chars}
  e.g.           fos_coolbrand_a1b2c3d4e5f6...

Key is returned ONCE at creation. Only the SHA-256 hash is stored.
The 16-char prefix is stored plaintext for fast lookup.

Usage in routes:
    brand: Brand = Depends(get_current_brand)
"""

import hashlib
import os
import secrets
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ApiKey, Brand
from db.session import get_session

ADMIN_SECRET = os.getenv("FASHIONOS_ADMIN_SECRET", "")


def generate_api_key(brand_id: str) -> tuple[str, str, str]:
    """
    Returns (full_key, prefix, sha256_hash).
    full_key  → shown to user once, never stored
    prefix    → stored plaintext for lookup narrowing
    key_hash  → stored in DB for verification
    """
    slug     = brand_id[:8].lower().replace("-", "").replace("_", "")
    prefix   = f"fos_{slug}"
    secret   = secrets.token_hex(24)          # 48 hex chars
    full_key = f"{prefix}_{secret}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, prefix, key_hash


async def get_current_brand(
    x_api_key: str = Header(..., alias="X-API-Key"),
    session:   AsyncSession = Depends(get_session),
) -> Brand:
    """
    FastAPI dependency: validates API key → returns associated Brand.
    Raises 401 on invalid key, 403 on inactive brand.
    """
    if not x_api_key or not x_api_key.startswith("fos_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format. Expected: fos_{brand}_{secret}",
        )

    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()

    key_rec = (await session.execute(
        select(ApiKey).where(
            ApiKey.key_hash  == key_hash,
            ApiKey.is_active == True,  # noqa: E712
        )
    )).scalar_one_or_none()

    if not key_rec:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
        )

    brand = (await session.execute(
        select(Brand).where(Brand.brand_id == key_rec.brand_id)
    )).scalar_one_or_none()

    if not brand or not brand.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand account is inactive or suspended.",
        )

    # Lazy update — non-blocking
    key_rec.last_used_at = datetime.now(timezone.utc)

    return brand


def require_admin(
    x_admin_secret: str = Header("", alias="X-Admin-Secret"),
) -> None:
    """Gate for brand-provisioning endpoints. Requires FASHIONOS_ADMIN_SECRET header."""
    if not ADMIN_SECRET:
        raise HTTPException(500, "FASHIONOS_ADMIN_SECRET not configured on server.")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(403, "Invalid admin secret.")