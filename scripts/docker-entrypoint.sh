#!/bin/bash
# FashionOS Docker Entrypoint
# Switches service mode based on first argument (or SERVICE_TYPE env var).
#
# Cloud Run deploys:
#   fashionos-api    → CMD=api    (default)
#   fashionos-worker → CMD=worker-beat (min-instances=1)
#
# Usage:
#   /docker-entrypoint.sh api          → uvicorn (FastAPI)
#   /docker-entrypoint.sh worker       → celery worker only
#   /docker-entrypoint.sh beat         → celery beat only
#   /docker-entrypoint.sh worker-beat  → celery worker + beat (single instance)
#   /docker-entrypoint.sh migrate      → alembic upgrade head (one-shot)

set -e

MODE="${1:-${SERVICE_TYPE:-api}}"

echo "[entrypoint] Starting FashionOS in mode: $MODE"

case "$MODE" in
  api)
    exec uvicorn api.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8080}" \
      --workers 1 \
      --log-level info
    ;;

  worker)
    exec celery -A api.workers.tasks worker \
      --loglevel=info \
      --pool=solo \
      --concurrency=1
    ;;

  beat)
    exec celery -A api.workers.tasks beat \
      --loglevel=info
    ;;

  worker-beat)
    # Single instance runs both worker + beat together.
    # ONLY safe with max-instances=1 on Cloud Run to avoid duplicate beat tasks.
    exec celery -A api.workers.tasks worker \
      --beat \
      --loglevel=info \
      --pool=solo \
      --concurrency=1
    ;;

  migrate)
    echo "[entrypoint] Running Alembic migrations..."
    exec alembic upgrade head
    ;;

  *)
    echo "[entrypoint] Unknown mode '$MODE'. Passing through to exec..."
    exec "$@"
    ;;
esac