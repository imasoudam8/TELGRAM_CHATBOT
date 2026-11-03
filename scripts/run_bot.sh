#!/usr/bin/env bash
set -euo pipefail

echo "==> Running database migrations via Alembic..."
alembic upgrade head

echo "==> Starting Telegram Bot application..."
exec python -m src.app.main
