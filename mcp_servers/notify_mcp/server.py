"""
notify-mcp — FashionOS MCP Server
Exposes Meta WhatsApp Cloud API + Resend email as MCP tools for brand owner notifications.

Tools:
  send_whatsapp_message   → Meta WhatsApp Cloud API (any recipient)
  send_critical_alert     → Pre-formatted critical alert to BRAND_OWNER_WHATSAPP
  send_restock_whatsapp   → WhatsApp to supplier with restock order details
  send_daily_digest       → Resend email to brand owner with run summary

Meta WhatsApp Cloud API setup:
  1. Create a Meta Business Account → WhatsApp Business → Add phone number
  2. Go to Meta for Developers → Your App → WhatsApp → API Setup
  3. Copy: Phone Number ID (WHATSAPP_PHONE_NUMBER_ID)
           Permanent System User Token (META_WHATSAPP_TOKEN)
  4. The phone number you message FROM is your registered business number.
  5. Phone numbers you message TO must be in international format without + prefix:
       Pakistan: 923001234567  (not +923001234567, not whatsapp:+923001234567)

  ⚠ Template message note:
    Outside a 24-hour customer-initiated window, WhatsApp requires approved
    message templates for business-initiated messages. For critical stockout
    alerts and daily digests you'll need a template approved in Meta Business
    Manager. During development the test number provided by Meta bypasses this.

Design:
  All tools are fire-and-forget — they return success/failure but never raise.
  If notify-mcp is unreachable the supervisor pipeline completes silently.

Port: 8005

Env vars required:
  META_WHATSAPP_TOKEN        → Permanent System User token (whatsapp_business_messaging permission)
  WHATSAPP_PHONE_NUMBER_ID   → Phone number ID from Meta App Dashboard (not the actual number)
  BRAND_OWNER_WHATSAPP       → Recipient number in international format WITHOUT + e.g. 923001234567
  RESEND_API_KEY             → from resend.com
  BRAND_OWNER_EMAIL          → founder's email for digests
  BRAND_NAME                 → used in message headers
  RESEND_FROM_DOMAIN         → e.g. fashionos.ai (optional, defaults to fashionos.ai)
  META_GRAPH_API_VERSION     → optional, defaults to v21.0 (same as ads-mcp)
"""

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

META_WHATSAPP_TOKEN      = os.getenv("META_WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
BRAND_OWNER_WHATSAPP     = os.getenv("BRAND_OWNER_WHATSAPP", "")   # e.g. 923001234567

RESEND_API_KEY    = os.getenv("RESEND_API_KEY", "")
BRAND_OWNER_EMAIL = os.getenv("BRAND_OWNER_EMAIL", "")
BRAND_NAME        = os.getenv("BRAND_NAME", "FashionOS Brand")

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v21.0")
WHATSAPP_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
RESEND_BASE_URL   = "https://api.resend.com"


# ── FastMCP app ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="notify-mcp",
    instructions=(
        "Send notifications to the brand owner via WhatsApp (Meta Cloud API) and email (Resend). "
        "All tools are fire-and-forget — failures are logged but never block the pipeline. "
        "Phone numbers must be in international format WITHOUT the + prefix: 923001234567. "
        "Use send_critical_alert for stockouts and urgent issues. "
        "Use send_daily_digest for end-of-day summaries. "
        "Use send_restock_whatsapp to notify suppliers of approved restock orders."
    ),
)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _whatsapp_post(to: str, message: str) -> dict:
    """
    POST a text message to Meta WhatsApp Cloud API.

    Args:
        to:      Recipient number in international format WITHOUT +.
                 Pakistan example: 923001234567
        message: Message body text.

    Returns raw API response dict.
    Raises httpx.HTTPStatusError on non-2xx so caller can catch and return error record.
    """
    if not META_WHATSAPP_TOKEN:
        raise ValueError("META_WHATSAPP_TOKEN not set. Add it to mcp_servers/notify_mcp/.env")
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise ValueError("WHATSAPP_PHONE_NUMBER_ID not set. Add it to mcp_servers/notify_mcp/.env")

    # Strip any accidental + prefix — Meta rejects +923..., wants 923...
    clean_to = to.lstrip("+")

    url     = f"{WHATSAPP_BASE_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to":                clean_to,
        "type":              "text",
        "text": {
            "preview_url": False,
            "body":        message[:4096],   # Meta limit is 4096 chars
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {META_WHATSAPP_TOKEN}",
                "Content-Type":  "application/json",
            },
        )
        if not r.is_success:
            # Grab Meta's actual error JSON before raising
            try:
                meta_error = r.json()
            except Exception:
                meta_error = r.text
            raise httpx.HTTPStatusError(
                f"Meta API {r.status_code}: {meta_error}",
                request=r.request,
                response=r,
            )
        return r.json()

async def _resend_post(payload: dict) -> dict:
    """POST to Resend email API."""
    if not RESEND_API_KEY:
        raise ValueError("RESEND_API_KEY not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{RESEND_BASE_URL}/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
        )
        r.raise_for_status()
        return r.json()


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def send_whatsapp_message(to: str, message: str) -> dict:
    """
    Send a WhatsApp message via Meta Cloud API to any recipient.

    Args:
        to:      Recipient phone number in international format WITHOUT the + prefix.
                 Pakistan: 923001234567
                 A leading + is automatically stripped if present.
        message: Message text. Max 4096 chars (Meta limit).

    Returns:
        {"success": true, "message_id": "wamid.XXXX", "to": "923001234567", "sent_at": "..."}
        {"success": false, "error": "...", "to": "..."}

    Never raises — always returns a dict with a success key.

    Used by: Supervisor send_notifications node for DM flag alerts.
    """
    try:
        result    = await _whatsapp_post(to, message)
        # Meta response: {"messages": [{"id": "wamid.XXXX"}], "contacts": [...]}
        message_id = result.get("messages", [{}])[0].get("id", "")
        return {
            "success":    True,
            "message_id": message_id,
            "to":         to.lstrip("+"),
            "sent_at":    datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "to": to}


@mcp.tool()
async def send_critical_alert(message: str, sku: Optional[str] = None) -> dict:
    """
    Send a critical alert WhatsApp to the brand owner (BRAND_OWNER_WHATSAPP env var).

    Pre-formats the message with a ⚠️ header and brand name.
    Recipient is read from BRAND_OWNER_WHATSAPP env var — no need to pass a number.

    Args:
        message: Alert body. Be specific: include SKU, numbers, action needed.
        sku:     Optional SKU this alert relates to (included in header if provided).

    Returns success dict (same shape as send_whatsapp_message).

    Used by: Supervisor send_notifications for stockouts, quality issues,
             high-priority DM flags (bulk_inquiry, complaint).
    """
    if not BRAND_OWNER_WHATSAPP:
        return {"success": False, "error": "BRAND_OWNER_WHATSAPP not configured in .env"}

    sku_tag   = f" [{sku}]" if sku else ""
    formatted = (
        f"⚠️ *{BRAND_NAME} — Critical Alert*{sku_tag}\n\n"
        f"{message}\n\n"
        f"_{datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')}_"
    )

    return await send_whatsapp_message(BRAND_OWNER_WHATSAPP, formatted)


@mcp.tool()
async def send_restock_whatsapp(
    supplier_number:  str,
    sku:              str,
    product_title:    str,
    quantity:         int,
    supplier_message: str,
) -> dict:
    """
    Send a WhatsApp restock order to a supplier via Meta Cloud API.

    Called when a restock recommendation is approved in the dashboard.
    Uses the pre-written supplier_message from the Restock Agent (Urdu-English mix).

    Args:
        supplier_number:  Supplier's WhatsApp number WITHOUT + prefix. e.g. 923001234567
        sku:              SKU being ordered (for audit log).
        product_title:    Product name (used in owner confirmation).
        quantity:         Units ordered.
        supplier_message: Full pre-written message from the Restock Agent.
                          Ready-to-send Urdu-English mix. No editing needed.

    Returns combined success status. Also pings BRAND_OWNER_WHATSAPP as confirmation.

    Used by: Approval endpoint PATCH /api/v1/restock/{id}/approve
    """
    # ── 1. Send to supplier ────────────────────────────────────────────────────
    supplier_result = await send_whatsapp_message(supplier_number, supplier_message)

    # ── 2. Confirm to brand owner ──────────────────────────────────────────────
    owner_notified = False
    if BRAND_OWNER_WHATSAPP:
        status_icon = "✅" if supplier_result.get("success") else "❌"
        owner_msg   = (
            f"📦 *Restock Order {status_icon}*\n\n"
            f"SKU: {sku}\n"
            f"Product: {product_title}\n"
            f"Quantity: {quantity} units\n"
            f"Supplier: {supplier_number}\n\n"
            f"_{datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')}_"
        )
        owner_result  = await send_whatsapp_message(BRAND_OWNER_WHATSAPP, owner_msg)
        owner_notified = owner_result.get("success", False)

    return {
        "success":           supplier_result.get("success", False),
        "supplier_notified": supplier_result.get("success", False),
        "owner_notified":    owner_notified,
        "sku":               sku,
        "quantity":          quantity,
        "sent_at":           datetime.now(timezone.utc).isoformat(),
        "error":             supplier_result.get("error"),
    }


@mcp.tool()
async def send_daily_digest(
    run_summary:     str,
    critical_count:  int,
    warning_count:   int,
    agents_run:      list[str],
    highlights:      list[str],
    pending_actions: list[str],
) -> dict:
    """
    Send the daily FashionOS run summary email to the brand owner.

    Args:
        run_summary:     The 2-4 sentence summary from the supervisor.
        critical_count:  Number of critical alerts this run.
        warning_count:   Number of warning alerts.
        agents_run:      List of agents that ran. e.g. ["inventory", "trend", ...]
        highlights:      Key wins/findings as a list of strings (bullet points).
        pending_actions: Items needing human approval as a list of strings.

    Sends HTML + plain-text email via Resend to BRAND_OWNER_EMAIL.
    WhatsApp is not used for the digest — email is the right channel for long-form summaries.

    Returns success dict with Resend email ID.

    Used by: Supervisor send_notifications node at end of daily sweep.
    """
    if not BRAND_OWNER_EMAIL:
        return {"success": False, "error": "BRAND_OWNER_EMAIL not configured in .env"}

    now      = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %d %B %Y")

    highlights_html = "".join(f"<li>{h}</li>" for h in highlights) or "<li>No highlights this run.</li>"
    pending_html    = "".join(f"<li>{p}</li>" for p in pending_actions) or "<li>Nothing pending — all good! ✅</li>"
    agents_str      = " → ".join(a.title() for a in agents_run)

    alert_color = "#dc2626" if critical_count > 0 else ("#d97706" if warning_count > 0 else "#16a34a")
    alert_text  = (
        f"{critical_count} critical, {warning_count} warnings"
        if (critical_count + warning_count) > 0
        else "All clear ✅"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; color: #111;">

  <h2 style="color: #111; border-bottom: 2px solid #f3f4f6; padding-bottom: 12px;">
    🤖 {BRAND_NAME} — Daily AI Run Report<br>
    <span style="font-size: 14px; font-weight: normal; color: #6b7280;">{date_str}</span>
  </h2>

  <div style="background: #f9fafb; border-radius: 8px; padding: 16px; margin: 16px 0;">
    <p style="margin: 0; font-size: 16px; line-height: 1.6;">{run_summary}</p>
  </div>

  <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
    <tr>
      <td style="padding: 8px 12px; background: #f3f4f6; border-radius: 6px; font-size: 13px; color: #374151;">
        <strong>Agents run:</strong> {agents_str}
      </td>
    </tr>
    <tr><td style="height: 8px;"></td></tr>
    <tr>
      <td style="padding: 8px 12px; background: {alert_color}20; border-left: 3px solid {alert_color};
                 border-radius: 0 6px 6px 0; font-size: 13px;">
        <strong style="color: {alert_color};">Alerts:</strong> {alert_text}
      </td>
    </tr>
  </table>

  <h3 style="margin-top: 24px; color: #374151;">✅ Highlights</h3>
  <ul style="color: #374151; line-height: 1.8; padding-left: 20px;">{highlights_html}</ul>

  <h3 style="margin-top: 24px; color: #374151;">⏳ Needs your attention</h3>
  <ul style="color: #374151; line-height: 1.8; padding-left: 20px;">{pending_html}</ul>

  <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
  <p style="font-size: 12px; color: #9ca3af; text-align: center;">
    FashionOS — Autonomous Fashion Brand OS · {now.strftime('%H:%M UTC')}
  </p>

</body>
</html>"""

    text_body = (
        f"FashionOS Daily Report — {date_str}\n\n"
        f"{run_summary}\n\n"
        f"Agents: {agents_str}\n"
        f"Alerts: {alert_text}\n\n"
        f"Highlights:\n" + "\n".join(f"• {h}" for h in highlights) + "\n\n"
        f"Pending:\n" + "\n".join(f"• {p}" for p in pending_actions)
    )

    try:
        result = await _resend_post({
            "from":    f"{BRAND_NAME} AI <digest@{os.getenv('RESEND_FROM_DOMAIN', 'fashionos.ai')}>",
            "to":      [BRAND_OWNER_EMAIL],
            "subject": f"[{BRAND_NAME}] Daily AI Report — {alert_text}",
            "html":    html_body,
            "text":    text_body,
        })
        return {
            "success":  True,
            "email_id": result.get("id"),
            "to":       BRAND_OWNER_EMAIL,
            "sent_at":  datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "to": BRAND_OWNER_EMAIL}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8005)
