import hmac, hashlib, base64, json
import os

WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")  # from Shopify Admin → Notifications → Webhooks
payload = json.dumps({"id": 12345, "name": "#1001", "line_items": [{"sku": "TEST-001", "quantity": 2}]})

sig = base64.b64encode(
    hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
).decode()

print("Sig:", sig)
print("Payload:", payload)

