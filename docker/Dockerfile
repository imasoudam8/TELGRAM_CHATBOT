# syntax=docker/dockerfile:1.4
# Alternative build location in docker/ folder matching root Dockerfile

FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.2 /uv /usr/local/bin/uv

COPY pyproject.toml .

RUN uv venv /opt/venv --python 3.12 && \
    uv pip install --python /opt/venv/bin/python --no-cache -r pyproject.toml

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY --chown=appuser:appgroup alembic.ini .
COPY --chown=appuser:appgroup migrations ./migrations
COPY --chown=appuser:appgroup scripts ./scripts
COPY --chown=appuser:appgroup src ./src

RUN chmod +x scripts/*.sh

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m src.app.infrastructure.healthcheck || exit 1

ENTRYPOINT ["./scripts/run_bot.sh"]
