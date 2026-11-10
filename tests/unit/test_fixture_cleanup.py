"""Unit tests verifying fixture teardown, engine disposal, and cleanup exception handling."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import disposable_postgres_db


@pytest.mark.asyncio
async def test_disposable_postgres_db_creation_failure_skips_drop() -> None:
    """Verify that if database creation fails, drop database is skipped but engine is disposed."""
    mock_engine = AsyncMock(spec=AsyncEngine)
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = RuntimeError("Failed to create database")
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with (
        patch("tests.conftest.is_ci_environment", return_value=True),
        patch("tests.conftest.create_async_engine", return_value=mock_engine),
        pytest.raises(RuntimeError, match="Failed to create database"),
    ):
        generator = disposable_postgres_db.__wrapped__()
        await generator.__anext__()

    # Verify connect was called once (for CREATE DATABASE)
    mock_engine.connect.assert_called_once()
    # Verify engine dispose was called
    mock_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_disposable_postgres_db_cleanup_failure_without_test_error() -> None:
    """Verify cleanup exception is raised when test passed but database drop fails."""
    mock_engine = AsyncMock(spec=AsyncEngine)
    mock_conn = AsyncMock()
    # First call (CREATE DATABASE) succeeds; second call (DROP DATABASE) fails
    mock_conn.execute.side_effect = [None, RuntimeError("Drop database permission denied")]
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with (
        patch("tests.conftest.is_ci_environment", return_value=True),
        patch("tests.conftest.create_async_engine", return_value=mock_engine),
        pytest.raises(RuntimeError, match="Drop database permission denied"),
    ):
        generator = disposable_postgres_db.__wrapped__()
        name, _settings = await generator.__anext__()
        assert name.startswith("test_db_")
        # Trigger fixture teardown normally
        await generator.__anext__()

    # Verify engine dispose was still called despite drop failure
    mock_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_disposable_postgres_db_preserves_test_exception_on_cleanup_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify original test exception propagates when teardown also encounters a cleanup error."""
    mock_engine = AsyncMock(spec=AsyncEngine)
    mock_conn = AsyncMock()
    # First call (CREATE) succeeds; second call (DROP) fails
    mock_conn.execute.side_effect = [None, RuntimeError("Teardown drop error")]
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with (
        patch("tests.conftest.is_ci_environment", return_value=True),
        patch("tests.conftest.create_async_engine", return_value=mock_engine),
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match="Primary test exception"),
    ):
        generator = disposable_postgres_db.__wrapped__()
        await generator.__anext__()
        # Simulate test throwing ValueError inside fixture block
        await generator.athrow(ValueError("Primary test exception"))

    # Verify primary test exception propagated, and cleanup failure was logged
    assert "Teardown drop error" in caplog.text
    mock_engine.dispose.assert_awaited_once()
