#!/usr/bin/env bash
set -euo pipefail

echo "==> Applying pending Alembic migrations..."
alembic upgrade head
echo "==> Migrations applied successfully."
