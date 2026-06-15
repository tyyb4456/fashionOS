# ─────────────────────────────────────────────────────────────────────────────
# ads-mcp — FashionOS MCP Server
# Exposes Meta Marketing API as MCP tools for the Marketing Agent.
#
# Build:  docker build -t fashionos/ads-mcp .
# Run:    docker run --env-file .env -p 8004:8004 fashionos/ads-mcp
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install \
    --default-timeout=300 \
    -r requirements.txt

COPY server.py .

EXPOSE 8004

CMD ["python", "server.py"]