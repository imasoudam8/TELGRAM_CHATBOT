"""Unit tests for operational health checks."""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.config.settings import Settings
from src.app.infrastructure.healthcheck import run_health_checks


@pytest.mark.asyncio
async def test_run_health_checks_all_healthy(test_settings: Settings) -> None:
    """Verify health checker reports 'healthy' when database and Redis respond."""
    with (
        patch("src.app.infrastructure.healthcheck.DatabaseManager") as mock_db_cls,
        patch("src.app.infrastructure.healthcheck.RedisManager") as mock_redis_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.initialize = AsyncMock()
        mock_db.validate_connection = AsyncMock(return_value=True)
        mock_db.close = AsyncMock()

        mock_redis = mock_redis_cls.return_value
        mock_redis.initialize = AsyncMock()
        mock_redis.validate_connection = AsyncMock(return_value=True)
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        assert is_healthy is True
        assert details["status"] == "healthy"
        assert details["database"]["status"] == "healthy"
        assert details["redis"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_run_health_checks_database_unhealthy(test_settings: Settings) -> None:
    """Verify health checker reports 'unhealthy' when PostgreSQL connection fails."""
    with (
        patch("src.app.infrastructure.healthcheck.DatabaseManager") as mock_db_cls,
        patch("src.app.infrastructure.healthcheck.RedisManager") as mock_redis_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.initialize = AsyncMock()
        mock_db.validate_connection = AsyncMock(
            side_effect=ConnectionRefusedError("Database offline")
        )
        mock_db.close = AsyncMock()

        mock_redis = mock_redis_cls.return_value
        mock_redis.initialize = AsyncMock()
        mock_redis.validate_connection = AsyncMock(return_value=True)
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        assert is_healthy is False
        assert details["status"] == "unhealthy"
        assert details["database"]["status"] == "unhealthy"
        assert "Database offline" in details["database"]["error"]
        assert details["redis"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_run_health_checks_redis_unhealthy(test_settings: Settings) -> None:
    """Verify health checker reports 'unhealthy' when Redis connection fails."""
    with (
        patch("src.app.infrastructure.healthcheck.DatabaseManager") as mock_db_cls,
        patch("src.app.infrastructure.healthcheck.RedisManager") as mock_redis_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.initialize = AsyncMock()
        mock_db.validate_connection = AsyncMock(return_value=True)
        mock_db.close = AsyncMock()

        mock_redis = mock_redis_cls.return_value
        mock_redis.initialize = AsyncMock()
        mock_redis.validate_connection = AsyncMock(side_effect=TimeoutError("Redis timeout"))
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        assert is_healthy is False
        assert details["status"] == "unhealthy"
        assert details["database"]["status"] == "healthy"
        assert details["redis"]["status"] == "unhealthy"
        assert "Redis timeout" in details["redis"]["error"]
