# syntax=docker/dockerfile:1.4
# Multi-stage Dockerfile for Anonymous Telegram Matching & Chat Bot

# ------------------------------------------------------------------------------
# Builder Stage: Install build dependencies and build wheels / virtualenv
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast, deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.6.2 /uv /usr/local/bin/uv

COPY pyproject.toml .

RUN uv venv /opt/venv --python 3.12 && \
    uv pip install --python /opt/venv/bin/python --no-cache -r pyproject.toml

# ------------------------------------------------------------------------------
# Final Runtime Stage: Minimal, secure non-root runtime
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL maintainer="Engineering Team"
LABEL description="Anonymous Telegram Matching & Chat Bot Runtime"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

# Install curl for health checks if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create secure non-root system user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source code and scripts
COPY --chown=appuser:appgroup alembic.ini .
COPY --chown=appuser:appgroup migrations ./migrations
COPY --chown=appuser:appgroup scripts ./scripts
COPY --chown=appuser:appgroup src ./src

RUN chmod +x scripts/*.sh

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -m src.app.infrastructure.healthcheck || exit 1

ENTRYPOINT ["./scripts/run_bot.sh"]
