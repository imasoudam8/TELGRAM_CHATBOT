"""Integration tests executing against live PostgreSQL and Redis services when provisioned."""

import socket

import pytest

from src.app.config.settings import get_settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager


def is_service_reachable(host: str, port: int) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, TimeoutError):
        return False


settings = get_settings()
postgres_live = is_service_reachable(settings.POSTGRES_HOST, settings.POSTGRES_PORT)
redis_live = is_service_reachable(settings.REDIS_HOST, settings.REDIS_PORT)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not postgres_live,
    reason="PostgreSQL service container not running on configured host:port",
)
async def test_live_postgres_connectivity() -> None:
    """Verify live PostgreSQL connectivity and ping query when provisioned (e.g. in CI)."""
    manager = DatabaseManager(settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not redis_live,
    reason="Redis service container not running on configured host:port",
)
async def test_live_redis_connectivity() -> None:
    """Verify live Redis connectivity and ping command when provisioned (e.g. in CI)."""
    manager = RedisManager(settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()
