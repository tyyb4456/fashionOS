"""
FashionOS Authentication — Clerk JWT only.
API keys removed. All routes use get_current_brand (Clerk JWT).
Admin routes use require_admin (X-Admin-Secret header).
"""

import os
from typing import Optional

from clerk_backend_api import Clerk, AuthenticateRequestOptions
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Brand
from db.session import get_session

ADMIN_SECRET  = os.getenv("FASHIONOS_ADMIN_SECRET", "")
CLERK_SECRET  = os.getenv("CLERK_SECRET_KEY", "")


FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:5173")
FRONTEND_URL_PROD  = os.getenv("FRONTEND_URL_PROD", "")

authorized = [FRONTEND_URL]
if FRONTEND_URL_PROD:
    authorized.append(FRONTEND_URL_PROD)
_clerk: Optional[Clerk] = None


def _get_clerk() -> Clerk:
    global _clerk
    if _clerk is None:
        if not CLERK_SECRET:
            raise RuntimeError("CLERK_SECRET_KEY not set.")
        _clerk = Clerk(bearer_auth=CLERK_SECRET)
    return _clerk


async def get_current_brand(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Brand:
    """
    Validates Clerk JWT → returns Brand.
    Frontend sends: Authorization: Bearer <clerk_token>
    """
    try:
        state = _get_clerk().authenticate_request(
            request,
            AuthenticateRequestOptions(authorized_parties=authorized),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Auth error: {exc}")

    if not state.is_signed_in:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    clerk_user_id = state.payload.get("sub")
    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Invalid token.")

    brand = (await session.execute(
        select(Brand).where(Brand.clerk_user_id == clerk_user_id)
    )).scalar_one_or_none()

    if not brand:
        raise HTTPException(
            status_code=404,
            detail="No brand found. Complete onboarding first.",
        )

    if not brand.is_active:
        raise HTTPException(status_code=403, detail="Brand account is inactive.")

    return brand


def require_admin(
    x_admin_secret: str = Header("", alias="X-Admin-Secret"),
) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(500, "FASHIONOS_ADMIN_SECRET not configured.")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(403, "Invalid admin secret.")