# ─────────────────────────────────────────────────────────────────────────────
# FashionOS — Root Dockerfile
# Single image used for all Python services:
#   fashionos-api     → CMD api        (FastAPI, Cloud Run serverless)
#   fashionos-worker  → CMD worker-beat (Celery, Cloud Run min=1)
#
# Build:  docker build -t fashionos/app .
# Run API: docker run --env-file .env -p 8080:8080 fashionos/app api
# Run Worker: docker run --env-file .env fashionos/app worker-beat
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# System deps — curl for healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first — this layer is cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install \
    --default-timeout=300 \
    -r requirements.txt

# Copy entrypoint script
COPY scripts/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Copy application code
COPY . .

# Cloud Run default port
EXPOSE 8080

ENTRYPOINT ["/docker-entrypoint.sh"]

# Default: run FastAPI API server
# Override with: docker run ... fashionos/app worker-beat
CMD ["api"]