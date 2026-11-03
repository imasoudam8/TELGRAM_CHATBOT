"""Integration tests for Redis client connection lifecycle and validation."""

from unittest.mock import AsyncMock, patch

import pytest
from redis.asyncio import Redis

from src.app.config.settings import Settings
from src.app.infrastructure.redis.client import RedisManager


@pytest.mark.asyncio
async def test_redis_manager_lifecycle(test_settings: Settings) -> None:
    """Verify RedisManager pool creation, ping validation, and clean teardown."""
    manager = RedisManager(test_settings)

    mock_client = AsyncMock(spec=Redis)
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()

    with patch("src.app.infrastructure.redis.client.Redis", return_value=mock_client):
        await manager.initialize()
        assert manager.client is not None

        # Validate ping
        pong = await manager.validate_connection()
        assert pong is True
        mock_client.ping.assert_awaited_once()

        # Teardown
        await manager.close()
        mock_client.aclose.assert_awaited_once()
