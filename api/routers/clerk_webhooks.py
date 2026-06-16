"""
Clerk Webhook Handler
Auto-creates Brand when user signs up via Clerk.

Setup in Clerk Dashboard:
  Webhooks → Add Endpoint → your-domain.com/api/v1/clerk/webhook
  Events: user.created, user.updated, user.deleted
  Copy Signing Secret → CLERK_WEBHOOK_SECRET in .env
"""

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from svix.webhooks import Webhook, WebhookVerificationError

from db.models import Brand
from db.session import get_session

router = APIRouter(prefix="/api/v1/clerk", tags=["clerk"])

CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET", "")


def _verify(payload: bytes, headers: dict) -> dict:
    if not CLERK_WEBHOOK_SECRET:
        raise HTTPException(500, "CLERK_WEBHOOK_SECRET not configured.")
    try:
        return Webhook(CLERK_WEBHOOK_SECRET).verify(payload, headers)
    except WebhookVerificationError:
        raise HTTPException(400, "Invalid webhook signature.")


def _extract_email(user: dict) -> str:
    emails     = user.get("email_addresses", [])
    primary_id = user.get("primary_email_address_id")
    for e in emails:
        if e.get("id") == primary_id:
            return e.get("email_address", "")
    return emails[0].get("email_address", "") if emails else ""


def _extract_name(user: dict) -> str:
    first = user.get("first_name", "")
    last  = user.get("last_name", "")
    if first or last:
        return f"{first} {last}".strip()
    email = _extract_email(user)
    return email.split("@")[0] if email else "My Brand"


@router.post("/webhook")
async def clerk_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    payload    = await request.body()
    event      = _verify(payload, dict(request.headers))
    event_type = event.get("type")
    user       = event.get("data", {})
    clerk_id   = user.get("id")

    # ── user.created → auto-provision Brand ───────────────────────────────────
    if event_type == "user.created":
        existing = (await session.execute(
            select(Brand).where(Brand.clerk_user_id == clerk_id)
        )).scalar_one_or_none()

        if not existing:
            email      = _extract_email(user)
            brand_name = _extract_name(user)
            brand_id   = f"brand_{clerk_id[:12].lower()}"

            session.add(Brand(
                id            = uuid.uuid4(),
                brand_id      = brand_id,
                brand_name    = brand_name,
                owner_email   = email,
                clerk_user_id = clerk_id,
                plan          = "starter",
                is_active     = True,
            ))
            await session.flush()
            print(f"[Clerk] ✓ Brand created: {brand_id} ({email})")

    # ── user.updated → sync email/name ────────────────────────────────────────
    elif event_type == "user.updated":
        brand = (await session.execute(
            select(Brand).where(Brand.clerk_user_id == clerk_id)
        )).scalar_one_or_none()

        if brand:
            brand.owner_email = _extract_email(user) or brand.owner_email
            brand.updated_at  = datetime.now(timezone.utc)

    # ── user.deleted → deactivate ──────────────────────────────────────────────
    elif event_type == "user.deleted":
        brand = (await session.execute(
            select(Brand).where(Brand.clerk_user_id == clerk_id)
        )).scalar_one_or_none()

        if brand:
            brand.is_active  = False
            brand.updated_at = datetime.now(timezone.utc)
            print(f"[Clerk] ✓ Brand deactivated: {brand.brand_id}")

    return {"received": True, "type": event_type}