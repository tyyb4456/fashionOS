"""
FashionOS — Webhook Test Script
================================
Generates the correct HMAC-SHA256 signature and fires a test webhook
at the local API server using Python requests (no PowerShell quoting issues).

Usage:
    python test_webhook.py
    python test_webhook.py --topic orders/cancelled
    python test_webhook.py --url http://localhost:8080
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys

import requests
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(): pass  # dotenv not installed — env vars must be set manually

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
BASE_URL        = "http://localhost:8080"
SHOP_DOMAIN     = os.getenv("SHOPIFY_SHOP_NAME", "fashionos-dev") + ".myshopify.com"

SAMPLE_PAYLOADS = {
    "orders/paid": {
        "id":         12345,
        "name":       "#1001",
        "line_items": [{"sku": "TEST-001", "quantity": 2}],
    },
    "orders/cancelled": {
        "id":         12346,
        "name":       "#1002",
        "line_items": [{"sku": "TEST-002", "quantity": 1}],
    },
    "inventory_levels/update": {
        "inventory_item_id": 99887766,
        "location_id":       12345678,
        "available":         50,
    },
}


# ── HMAC signing ──────────────────────────────────────────────────────────────

def sign(body_bytes: bytes, secret: str) -> str:
    """Returns base64-encoded HMAC-SHA256 of body_bytes using secret."""
    if not secret:
        return "no-secret-configured"
    digest = hmac.new(
        secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fire a test Shopify webhook")
    parser.add_argument("--topic", default="orders/paid",
                        help="Shopify topic (default: orders/paid)")
    parser.add_argument("--url", default=BASE_URL,
                        help=f"Base URL (default: {BASE_URL})")
    args = parser.parse_args()

    topic   = args.topic
    payload = SAMPLE_PAYLOADS.get(topic, {"test": True, "topic": topic})

    # Serialize exactly — these bytes are what gets signed AND sent
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature  = sign(body_bytes, WEBHOOK_SECRET)

    endpoint = f"{args.url}/api/v1/webhooks/shopify/{topic}"

    print("=" * 60)
    print(f"  Topic   : {topic}")
    print(f"  Endpoint: {endpoint}")
    print(f"  Payload : {body_bytes.decode()}")
    print(f"  Sig     : {signature}")
    if not WEBHOOK_SECRET:
        print("  ⚠ SHOPIFY_WEBHOOK_SECRET not set — HMAC will be bypassed (dev mode)")
    print("=" * 60)

    resp = requests.post(
        endpoint,
        data=body_bytes,
        headers={
            "Content-Type":           "application/json",
            "X-Shopify-Topic":        topic,
            "X-Shopify-Hmac-Sha256":  signature,
            "X-Shopify-Shop-Domain":  SHOP_DOMAIN,
        },
        timeout=10,
    )

    print(f"\n  HTTP {resp.status_code}")
    try:
        print(f"  {json.dumps(resp.json(), indent=2)}")
    except Exception:
        print(f"  {resp.text}")

    if resp.status_code == 200:
        print("\n  🗸 Webhook accepted — Celery task dispatched.")
    elif resp.status_code == 401:
        print("\n  🗴 Signature mismatch — check SHOPIFY_WEBHOOK_SECRET in .env")
    elif resp.status_code == 400:
        print("\n  🗴 Bad request — check payload format")
    else:
        print(f"\n  ⚠ Unexpected status: {resp.status_code}")

    sys.exit(0 if resp.status_code == 200 else 1)


if __name__ == "__main__":
    main()
