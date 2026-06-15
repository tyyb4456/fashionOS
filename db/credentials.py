"""
FashionOS Credential Manager
==============================
Handles Fernet encryption of brand credentials in PostgreSQL
and the Redis credential cache consumed by MCP servers.

Redis key: fashionos:creds:{brand_id}  →  JSON of decrypted credentials
No TTL — invalidated explicitly on credential update / brand delete.

Generate encryption key:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import json
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_KEY = os.getenv("FASHIONOS_ENCRYPTION_KEY", "")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_cipher: Optional[Fernet] = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError(
                "FASHIONOS_ENCRYPTION_KEY not set. "
                "Run: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        _cipher = Fernet(ENCRYPTION_KEY.encode())
    return _cipher


def encrypt_value(value: str) -> str:
    """Encrypt a plaintext string. Returns '' for empty input."""
    if not value:
        return ""
    return _get_cipher().encrypt(value.encode()).decode()


def decrypt_value(encrypted: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns '' on failure."""
    if not encrypted:
        return ""
    try:
        return _get_cipher().decrypt(encrypted.encode()).decode()
    except (InvalidToken, Exception):
        return ""


class BrandCredentials:
    __slots__ = (
        # Shopify — brand-specific
        "shopify_shop_name",
        "shopify_access_token",
        "shopify_webhook_secret",
        # Meta Ads — brand-specific
        "meta_access_token",
        "meta_ad_account_id",
        # Instagram DMs — brand-specific
        "instagram_access_token",
        "instagram_page_id",
        # Notification recipients — where to SEND alerts for this brand
        "brand_owner_whatsapp",
        "brand_owner_email",
    )
    
    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, ""))

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


async def cache_brand_credentials(brand_id: str, creds: BrandCredentials) -> None:
    """
    Write decrypted credentials to Redis for MCP server hot-path access.
    Called after brand create / credential update.
    """
    import redis.asyncio as aioredis
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.set(f"fashionos:creds:{brand_id}", json.dumps(creds.to_dict()))
    finally:
        await r.aclose()


async def invalidate_brand_credentials(brand_id: str) -> None:
    """Remove brand credentials from Redis (on delete or key rotation)."""
    import redis.asyncio as aioredis
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.delete(f"fashionos:creds:{brand_id}")
    finally:
        await r.aclose()