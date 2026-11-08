"""Unit tests for the application lifecycle: startup ordering, failure cleanup, and shutdown."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.lifecycle import application_lifespan
from src.app.infrastructure.redis.client import RedisManager


@pytest.mark.asyncio
async def test_successful_startup_ordering(test_settings: Settings) -> None:
    """Verify strict startup ordering:

    1. PostgreSQL initialized
    2. PostgreSQL validated
    3. Redis initialized
    4. Redis validated
    5. Bot and dispatcher ready
    6. Teardown runs in reverse order
    """
    call_log: list[str] = []

    mock_db = MagicMock(spec=DatabaseManager)

    async def db_init() -> None:
        call_log.append("db_init")

    async def db_val() -> bool:
        call_log.append("db_val")
        return True

    async def db_close() -> None:
        call_log.append("db_close")

    mock_db.initialize = AsyncMock(side_effect=db_init)
    mock_db.validate_connection = AsyncMock(side_effect=db_val)
    mock_db.close = AsyncMock(side_effect=db_close)

    mock_redis = MagicMock(spec=RedisManager)

    async def redis_init() -> None:
        call_log.append("redis_init")

    async def redis_val() -> bool:
        call_log.append("redis_val")
        return True

    async def redis_close() -> None:
        call_log.append("redis_close")

    mock_redis.initialize = AsyncMock(side_effect=redis_init)
    mock_redis.validate_connection = AsyncMock(side_effect=redis_val)
    mock_redis.close = AsyncMock(side_effect=redis_close)

    mock_bot = MagicMock(spec=Bot)
    mock_bot.session = MagicMock()

    async def bot_close() -> None:
        call_log.append("bot_close")

    mock_bot.session.close = AsyncMock(side_effect=bot_close)

    with patch("src.app.infrastructure.lifecycle.create_bot", return_value=mock_bot):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ) as ctx:
            call_log.append("polling_start")
            assert ctx.bot is mock_bot
            assert ctx.db_manager is mock_db
            assert ctx.redis_manager is mock_redis

    expected_order = [
        "db_init",
        "db_val",
        "redis_init",
        "redis_val",
        "polling_start",
        "bot_close",
        "redis_close",
        "db_close",
    ]
    assert call_log == expected_order


@pytest.mark.asyncio
async def test_postgres_initialization_failure(test_settings: Settings) -> None:
    """Verify that PostgreSQL connectivity failure aborts startup without leaking."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock()
    mock_db.validate_connection = AsyncMock(
        side_effect=ConnectionRefusedError("PostgreSQL connection refused")
    )
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.close = AsyncMock()

    with pytest.raises(ConnectionRefusedError, match="PostgreSQL connection refused"):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ):
            pytest.fail("Lifespan context should not be entered on DB failure")

    # DB was initialized, so it must be cleaned up
    mock_db.close.assert_awaited_once()
    # Redis should never have been initialized
    mock_redis.initialize.assert_not_called()


@pytest.mark.asyncio
async def test_redis_initialization_failure(test_settings: Settings) -> None:
    """Verify that Redis failure cleans up the previously initialized PostgreSQL engine."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock()
    mock_db.validate_connection = AsyncMock(return_value=True)
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.validate_connection = AsyncMock(
        side_effect=TimeoutError("Redis connection timed out")
    )
    mock_redis.close = AsyncMock()

    with pytest.raises(TimeoutError, match="Redis connection timed out"):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ):
            pytest.fail("Lifespan context should not be entered on Redis failure")

    # Critical: PostgreSQL was initialized before Redis; it MUST be cleaned up!
    mock_db.close.assert_awaited_once()
    # Redis partially initialized, so its cleanup must also run
    mock_redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_after_partial_initialization(test_settings: Settings) -> None:
    """Verify partial failure during DB initialization cleans up cleanly without errors."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock(side_effect=RuntimeError("Engine pool creation failed"))
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.close = AsyncMock()

    with pytest.raises(RuntimeError, match="Engine pool creation failed"):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ):
            pass

    mock_db.close.assert_awaited_once()
    mock_redis.initialize.assert_not_called()
    mock_redis.close.assert_not_called()


@pytest.mark.asyncio
async def test_graceful_shutdown_on_cancellation(test_settings: Settings) -> None:
    """Verify that an asyncio.CancelledError triggers full graceful cleanup."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock()
    mock_db.validate_connection = AsyncMock(return_value=True)
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.validate_connection = AsyncMock(return_value=True)
    mock_redis.close = AsyncMock()

    mock_bot = MagicMock(spec=Bot)
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with (
        patch("src.app.infrastructure.lifecycle.create_bot", return_value=mock_bot),
        pytest.raises(asyncio.CancelledError),
    ):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ):
            raise asyncio.CancelledError()

    mock_bot.session.close.assert_awaited_once()
    mock_redis.close.assert_awaited_once()
    mock_db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_ownership_exactly_once_resource_cleanup(test_settings: Settings) -> None:
    """Verify resources are closed exactly once when both Dispatcher shutdown and lifespan run."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock()
    mock_db.validate_connection = AsyncMock(return_value=True)
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.validate_connection = AsyncMock(return_value=True)
    mock_redis.close = AsyncMock()

    mock_bot = MagicMock(spec=Bot)
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with patch("src.app.infrastructure.lifecycle.create_bot", return_value=mock_bot):
        async with application_lifespan(
            test_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ) as ctx:
            # Simulate Dispatcher emitting shutdown during polling teardown
            await ctx.dispatcher.emit_shutdown(bot=ctx.bot)
            # Dispatcher hook must NOT dispose resources
            mock_bot.session.close.assert_not_called()
            mock_redis.close.assert_not_called()
            mock_db.close.assert_not_called()

    # After lifespan context finishes, resources MUST be closed exactly once
    mock_bot.session.close.assert_awaited_once()
    mock_redis.close.assert_awaited_once()
    mock_db.close.assert_awaited_once()
