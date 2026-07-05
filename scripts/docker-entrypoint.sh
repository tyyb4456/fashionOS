#!/bin/sh
set -e

case "$1" in
  api)
    exec uvicorn api.main:app --host 0.0.0.0 --port 8080
    ;;
  worker-beat)
    exec celery -A api.workers.tasks worker --beat --loglevel=info
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "Unknown command: $1"
    echo "Usage: docker run <image> [api|worker-beat|migrate]"
    exit 1
    ;;
esac
