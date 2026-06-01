import httpx
import json

BASE = "http://localhost:8001/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

# Step 1 — initialize session
r = httpx.post(BASE, headers=HEADERS, json={
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"}
    }
})
session_id = r.headers.get("mcp-session-id")
print(f"✅ Session ID: {session_id}\n")

# Step 2 — call list_products
HEADERS["mcp-session-id"] = session_id
r2 = httpx.post(BASE, headers=HEADERS, json={
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "list_products",
        "arguments": {"limit": 3}
    }
})

# SSE response — parse each "data: {...}" line
print(f"Status: {r2.status_code}")
print(f"Raw response:\n{r2.text}\n")

# Parse SSE lines
for line in r2.text.splitlines():
    if line.startswith("data: "):
        payload = line[6:]  # strip "data: "
        try:
            parsed = json.loads(payload)
            print("✅ Parsed result:")
            print(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            pass