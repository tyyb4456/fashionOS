"""
FashionOS — Approval Endpoints
================================
PATCH endpoints for human approval of agent-recommended actions.
These are the "Approve" buttons in the dashboard.

Routes:
  PATCH /api/v1/pricing/{record_id}/approve    ← execute markdown via Shopify
  PATCH /api/v1/pricing/{record_id}/reject     ← mark rejected, no Shopify call
  PATCH /api/v1/marketing/{record_id}/approve  ← execute budget change / activate via Meta
  PATCH /api/v1/marketing/{record_id}/reject   ← mark rejected
  PATCH /api/v1/restock/{record_id}/approve    ← mark approved + send WhatsApp to supplier
  PATCH /api/v1/restock/{record_id}/reject     ← mark rejected
  PATCH /api/v1/content/{record_id}/status     ← mark posted / skipped

Session 8 — multi-tenancy hardening:
  Every route now requires `brand: Brand = Depends(get_current_brand)`.
  After fetching a record by id, we verify `rec.brand_id == brand.brand_id`
  before doing anything else — 404 if missing, 403 if it belongs to another
  brand. brand_id is then threaded into the crud mark_*/update_* calls as a
  second ownership guard in the WHERE clause (defense in depth — even a bug
  in the route check can't let one brand mutate another brand's records).

Design:
  - All approval endpoints make live MCP API calls inline (FastAPI async, no Celery).
  - MCP errors return 502 with details — DB record stays unchanged so retry is safe.
  - DB updates use crud update helpers (brand-scoped).
  - Rejection endpoints just update the DB — no external API call needed.
  - Restock approval optionally sends WhatsApp via notify-mcp if NOTIFY_MCP_URL is set.
"""

import json
import os
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db import crud
from db.schemas import MarketingActionSchema, PricingActionSchema, RestockRecommendationSchema
from db.session import get_session

from api.auth import get_current_brand
from db.models import Brand

router = APIRouter(prefix="/api/v1", tags=["approvals"])

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
ADS_MCP_URL     = os.getenv("ADS_MCP_URL",     "http://localhost:8004/mcp")
NOTIFY_MCP_URL  = os.getenv("NOTIFY_MCP_URL",  "http://localhost:8005/mcp")


# ── MCP helper ────────────────────────────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0]:
        return json.loads(raw[0]["text"])
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    content = getattr(raw, "content", str(raw))
    return json.loads(content) if isinstance(content, str) else content


# ── Request bodies ─────────────────────────────────────────────────────────────

class RestockApproveBody(BaseModel):
    supplier_whatsapp: Optional[str] = None  # whatsapp:+923XXXXXXXXX


class ContentStatusBody(BaseModel):
    new_status: str  # "posted" | "skipped"


# ══════════════════════════════════════════════════════════════════════════════
# PRICING APPROVALS
# ══════════════════════════════════════════════════════════════════════════════

@router.patch(
    "/pricing/{record_id}/approve",
    response_model=PricingActionSchema,
    summary="Approve a pending pricing decision",
    description=(
        "Executes the markdown/increase/clearance on Shopify via shopify-mcp. "
        "Marks the DB record as auto_executed=True. "
        "Only works on records where auto_executed=False."
    ),
)
async def approve_pricing_decision(
    record_id: UUID,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> PricingActionSchema:
    rec = await crud.get_pricing_action(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pricing record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.auto_executed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already executed.")
    if rec.action == "hold":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Hold actions don't need approval.")

    # ── Call Shopify MCP ───────────────────────────────────────────────────────
    try:
        client   = MultiServerMCPClient({"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}})
        tools    = await client.get_tools()
        tool_map = {t.name: t for t in tools}

        if "update_product_price" not in tool_map:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="shopify-mcp unavailable.")

        raw = await tool_map["update_product_price"].ainvoke({
            "variant_id":       rec.variant_id,
            "new_price":        rec.recommended_price,
            "compare_at_price": rec.new_compare_at_price,
            "reason":           f"[APPROVED] {rec.reason or rec.action}",
            "brand_id":         brand.brand_id,
        })
        result = _parse_mcp_result(raw)
        if isinstance(result, dict) and not result.get("success", True) is False:
            pass  # Shopify returns the variant object on success, not a success key
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Shopify MCP call failed: {exc}",
        )

    # ── Update DB ──────────────────────────────────────────────────────────────
    updated = await crud.mark_pricing_executed(session, record_id=str(record_id), brand_id=brand.brand_id)
    return PricingActionSchema.model_validate(updated)


@router.patch(
    "/pricing/{record_id}/reject",
    response_model=PricingActionSchema,
    summary="Reject a pending pricing decision",
    description="Marks the record as rejected. No Shopify call is made.",
)
async def reject_pricing_decision(
    record_id: UUID,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> PricingActionSchema:
    rec = await crud.get_pricing_action(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pricing record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.auto_executed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already executed — cannot reject.")

    updated = await crud.mark_pricing_rejected(session, record_id=str(record_id), brand_id=brand.brand_id)
    return PricingActionSchema.model_validate(updated)


# ══════════════════════════════════════════════════════════════════════════════
# MARKETING APPROVALS
# ══════════════════════════════════════════════════════════════════════════════

@router.patch(
    "/marketing/{record_id}/approve",
    response_model=MarketingActionSchema,
    summary="Approve a pending marketing budget change",
    description=(
        "Executes the budget increase or campaign activation via ads-mcp. "
        "Only increase_budget and activate actions are pending; "
        "pause and decrease_budget are already auto-executed."
    ),
)
async def approve_marketing_decision(
    record_id: UUID,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> MarketingActionSchema:
    rec = await crud.get_marketing_action(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketing record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.auto_executed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already executed.")
    if rec.action not in ("increase_budget", "activate"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Action '{rec.action}' cannot be approved here.")

    # ── Call Meta MCP ──────────────────────────────────────────────────────────
    try:
        client   = MultiServerMCPClient({"ads": {"url": ADS_MCP_URL, "transport": "streamable_http"}})
        tools    = await client.get_tools()
        tool_map = {t.name: t for t in tools}

        if rec.action == "increase_budget":
            if "update_campaign_budget" not in tool_map:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ads-mcp unavailable.")
            if not rec.new_budget_pkr:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="new_budget_pkr not set on this record.")

            raw = await tool_map["update_campaign_budget"].ainvoke({
                "campaign_id":          rec.campaign_id,
                "new_daily_budget_pkr": rec.new_budget_pkr,
                "reason":               f"[APPROVED] {rec.reason or 'Budget increase approved by founder.'}",
                "brand_id":             brand.brand_id,
            })

        elif rec.action == "activate":
            if "activate_campaign" not in tool_map:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ads-mcp unavailable.")
            raw = await tool_map["activate_campaign"].ainvoke({
                "campaign_id": rec.campaign_id,
                "reason":      f"[APPROVED] {rec.reason or 'Campaign activation approved by founder.'}",
                "brand_id":    brand.brand_id,
            })

        result = _parse_mcp_result(raw)
        if isinstance(result, dict) and result.get("success") is False:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error", "Meta API call failed."))

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"ads-mcp call failed: {exc}")

    updated = await crud.mark_marketing_executed(session, record_id=str(record_id), brand_id=brand.brand_id)
    return MarketingActionSchema.model_validate(updated)


@router.patch(
    "/marketing/{record_id}/reject",
    response_model=MarketingActionSchema,
    summary="Reject a pending marketing decision",
)
async def reject_marketing_decision(
    record_id: UUID,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> MarketingActionSchema:
    rec = await crud.get_marketing_action(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketing record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.auto_executed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already executed.")

    updated = await crud.mark_marketing_rejected(session, record_id=str(record_id), brand_id=brand.brand_id)
    return MarketingActionSchema.model_validate(updated)


# ══════════════════════════════════════════════════════════════════════════════
# RESTOCK APPROVALS
# ══════════════════════════════════════════════════════════════════════════════

@router.patch(
    "/restock/{record_id}/approve",
    response_model=RestockRecommendationSchema,
    summary="Approve a restock order",
    description=(
        "Marks the restock as approved and optionally sends the supplier WhatsApp "
        "message via notify-mcp. If supplier_whatsapp is not provided in the body, "
        "only the DB status is updated."
    ),
)
async def approve_restock_order(
    record_id: UUID,
    body:      RestockApproveBody = RestockApproveBody(),
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession       = Depends(get_session),
) -> RestockRecommendationSchema:
    rec = await crud.get_restock_recommendation(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restock record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.status not in ("pending_approval",):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Already in status '{rec.status}'.")

    # ── Optional: send WhatsApp to supplier ────────────────────────────────────
    whatsapp_sent = False
    if body.supplier_whatsapp and rec.supplier_message:
        try:
            client   = MultiServerMCPClient({"notify": {"url": NOTIFY_MCP_URL, "transport": "streamable_http"}})
            tools    = await client.get_tools()
            tool_map = {t.name: t for t in tools}

            if "send_restock_whatsapp" in tool_map:
                raw = await tool_map["send_restock_whatsapp"].ainvoke({
                    "brand_id":         brand.brand_id,
                    "supplier_number":  body.supplier_whatsapp,
                    "sku":              rec.sku,
                    "product_title":    "",  # not stored on record
                    "quantity":         rec.recommended_quantity,
                    "supplier_message": rec.supplier_message,
                })
                result = _parse_mcp_result(raw)
                whatsapp_sent = isinstance(result, dict) and result.get("success", False)
                if whatsapp_sent:
                    print(f"[Approvals] ✓ Restock WhatsApp sent for {rec.sku} to {body.supplier_whatsapp}")
        except Exception as exc:
            print(f"[Approvals] ✗ notify-mcp WhatsApp failed: {exc} — continuing with DB update")

    # ── Update DB ──────────────────────────────────────────────────────────────
    new_status = "ordered" if whatsapp_sent else "approved"
    updated    = await crud.update_restock_status(
        session, record_id=str(record_id), new_status=new_status, brand_id=brand.brand_id
    )
    return RestockRecommendationSchema.model_validate(updated)


@router.patch(
    "/restock/{record_id}/reject",
    response_model=RestockRecommendationSchema,
    summary="Reject a restock order",
)
async def reject_restock_order(
    record_id: UUID,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
) -> RestockRecommendationSchema:
    rec = await crud.get_restock_recommendation(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restock record not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")
    if rec.status not in ("pending_approval",):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Already in status '{rec.status}'.")

    updated = await crud.update_restock_status(
        session, record_id=str(record_id), new_status="cancelled", brand_id=brand.brand_id
    )
    return RestockRecommendationSchema.model_validate(updated)


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT STATUS
# ══════════════════════════════════════════════════════════════════════════════

@router.patch(
    "/content/{record_id}/status",
    summary="Update content post status",
    description="Mark a content post as posted or skipped. Accepted values: posted | skipped",
)
async def update_content_status(
    record_id: UUID,
    body:      ContentStatusBody,
    brand:     Brand = Depends(get_current_brand),
    session:   AsyncSession = Depends(get_session),
):
    if body.new_status not in ("posted", "skipped"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="new_status must be 'posted' or 'skipped'.")

    rec = await crud.get_content_post(session, record_id=str(record_id))
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content post not found.")
    if rec.brand_id != brand.brand_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this record.")

    updated = await crud.update_content_post_status(
        session, record_id=str(record_id), new_status=body.new_status, brand_id=brand.brand_id
    )
    return {"id": str(record_id), "status": body.new_status, "sku": rec.sku}