# ─────────────────────────────────────────────────────────────────────────────
# social-mcp — FashionOS MCP Server
# Exposes TikTok + Instagram scraping (via Apify) as MCP tools.
#
# Build:  docker build -t fashionos/social-mcp .
# Run:    docker run --env-file .env -p 8002:8002 fashionos/social-mcp
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

EXPOSE 8002

CMD ["python", "server.py"]