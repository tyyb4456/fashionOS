# ─────────────────────────────────────────────────────────────────────────────
# shopify-mcp — FashionOS MCP Server
# Exposes Shopify Admin API as MCP tools to all FashionOS agents.
#
# Build:  docker build -t fashionos/shopify-mcp .
# Run:    docker run --env-file .env -p 8001:8001 fashionos/shopify-mcp
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# curl needed for docker-compose healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first — this layer is cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY server.py .

EXPOSE 8001

# FastMCP runs with streamable-http transport on port 8001
CMD ["python", "server.py"]