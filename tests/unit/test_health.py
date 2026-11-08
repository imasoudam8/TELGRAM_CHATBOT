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


@pytest.mark.asyncio
async def test_run_health_checks_postgres_cleanup_failure_does_not_prevent_redis_check(
    test_settings: Settings,
) -> None:
    """Verify that a failure during PostgreSQL cleanup does not prevent the Redis check."""
    with (
        patch("src.app.infrastructure.healthcheck.DatabaseManager") as mock_db_cls,
        patch("src.app.infrastructure.healthcheck.RedisManager") as mock_redis_cls,
    ):
        mock_db = mock_db_cls.return_value
        mock_db.initialize = AsyncMock()
        mock_db.validate_connection = AsyncMock(return_value=True)
        mock_db.close = AsyncMock(side_effect=RuntimeError("Postgres close socket error"))

        mock_redis = mock_redis_cls.return_value
        mock_redis.initialize = AsyncMock()
        mock_redis.validate_connection = AsyncMock(return_value=True)
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        # Redis check must execute and succeed despite Postgres close failure
        mock_redis.validate_connection.assert_awaited_once()
        mock_redis.close.assert_awaited_once()
        assert details["redis"]["status"] == "healthy"
        assert details["database"]["status"] == "healthy"
        assert is_healthy is True


@pytest.mark.asyncio
async def test_run_health_checks_redis_cleanup_guaranteed_on_validation_failure(
    test_settings: Settings,
) -> None:
    """Verify that Redis connection cleanup is guaranteed even when Redis validation fails."""
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
        mock_redis.validate_connection = AsyncMock(side_effect=ConnectionError("Redis failed"))
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        assert is_healthy is False
        mock_redis.close.assert_awaited_once()
        assert details["redis"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_run_health_checks_sanitizes_credentials_in_errors(test_settings: Settings) -> None:
    """Verify that health check error output scrubs credentials and sensitive connection URIs."""
    with (
        patch("src.app.infrastructure.healthcheck.DatabaseManager") as mock_db_cls,
        patch("src.app.infrastructure.healthcheck.RedisManager") as mock_redis_cls,
    ):
        raw_db_pass = test_settings.POSTGRES_PASSWORD.get_secret_value()
        raw_token = test_settings.BOT_TOKEN.get_secret_value()

        mock_db = mock_db_cls.return_value
        mock_db.initialize = AsyncMock()
        mock_db.validate_connection = AsyncMock(
            side_effect=RuntimeError(
                f"Failed connection to postgresql+asyncpg://postgres:{raw_db_pass}@"
                f"localhost:5432/db with token {raw_token}"
            )
        )
        mock_db.close = AsyncMock()

        mock_redis = mock_redis_cls.return_value
        mock_redis.initialize = AsyncMock()
        mock_redis.validate_connection = AsyncMock(
            side_effect=RuntimeError("Auth failed on redis://:test_redis_pass@localhost:6379/15")
        )
        mock_redis.close = AsyncMock()

        is_healthy, details = await run_health_checks(test_settings)

        assert is_healthy is False
        db_error = details["database"]["error"]
        assert raw_db_pass not in db_error
        assert raw_token not in db_error
        assert "[REDACTED_DB_PASSWORD]" in db_error
        assert "[REDACTED_BOT_TOKEN]" in db_error

        redis_error = details["redis"]["error"]
        assert "test_redis_pass" not in redis_error
        assert "[REDACTED_REDIS_PASSWORD]" in redis_error


def test_healthcheck_cli_exit_codes(test_settings: Settings) -> None:
    """Verify that CLI main() exits with 0 on healthy and 1 on unhealthy."""
    from src.app.infrastructure.healthcheck import main

    with (
        patch("src.app.infrastructure.healthcheck.get_settings", return_value=test_settings),
        patch("src.app.infrastructure.healthcheck.configure_logging"),
    ):
        # Case 1: Healthy -> exit code 0
        with (
            patch(
                "src.app.infrastructure.healthcheck.run_health_checks",
                return_value=(True, {"status": "healthy"}),
            ),
            pytest.raises(SystemExit) as exc1,
        ):
            main()
        assert exc1.value.code == 0

        # Case 2: Unhealthy -> exit code 1
        with (
            patch(
                "src.app.infrastructure.healthcheck.run_health_checks",
                return_value=(False, {"status": "unhealthy"}),
            ),
            pytest.raises(SystemExit) as exc2,
        ):
            main()
        assert exc2.value.code == 1
