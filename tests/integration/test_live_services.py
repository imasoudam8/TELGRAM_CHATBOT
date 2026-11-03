"""Integration tests executing against live PostgreSQL and Redis service containers."""

import os
import socket

import pytest

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager


def is_service_reachable(host: str, port: int) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, TimeoutError):
        return False


def is_ci_environment() -> bool:
    """Detect whether tests are executing within a CI environment (e.g., GitHub Actions)."""
    return os.getenv("CI", "").lower() in ("true", "1") or os.getenv("GITHUB_ACTIONS") == "true"


@pytest.fixture
def live_service_settings() -> Settings:
    """Provide fresh settings read from environment variables without fixture overrides."""
    return Settings()


@pytest.mark.asyncio
async def test_live_postgres_connectivity(live_service_settings: Settings) -> None:
    """Verify live PostgreSQL connectivity and ping query when provisioned.

    In CI runners, this test is mandatory and will fail if the PostgreSQL service is unavailable.
    In local developer environments without live containers, this test is skipped cleanly.
    """
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.POSTGRES_HOST, live_service_settings.POSTGRES_PORT
    ):
        pytest.skip(
            f"PostgreSQL service not reachable on {live_service_settings.POSTGRES_HOST}:"
            f"{live_service_settings.POSTGRES_PORT} (local test environment)"
        )

    manager = DatabaseManager(live_service_settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_redis_connectivity(live_service_settings: Settings) -> None:
    """Verify live Redis connectivity and ping command when provisioned.

    In CI runners, this test is mandatory and will fail if the Redis service is unavailable.
    In local developer environments without live containers, this test is skipped cleanly.
    """
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.REDIS_HOST, live_service_settings.REDIS_PORT
    ):
        pytest.skip(
            f"Redis service not reachable on {live_service_settings.REDIS_HOST}:"
            f"{live_service_settings.REDIS_PORT} (local test environment)"
        )

    manager = RedisManager(live_service_settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()
