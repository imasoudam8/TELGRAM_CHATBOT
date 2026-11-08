"""Integration tests for Telegram bot and dispatcher initialization and lifecycles."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.enums import ParseMode

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.infrastructure.telegram.bot import create_bot, create_dispatcher


def test_create_bot_initialization(test_settings: Settings) -> None:
    """Verify bot client is created with HTML parse mode and configured token."""
    bot = create_bot(test_settings)
    assert isinstance(bot, Bot)
    assert bot.token == test_settings.BOT_TOKEN.get_secret_value()
    assert bot.default.parse_mode == ParseMode.HTML


@pytest.mark.asyncio
async def test_dispatcher_lifecycle_hooks(test_settings: Settings) -> None:
    """Verify startup and shutdown hooks execute cleanly and coordinate resources."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.validate_connection = AsyncMock(return_value=True)
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.validate_connection = AsyncMock(return_value=True)
    mock_redis.close = AsyncMock()

    dp = create_dispatcher(test_settings, db_manager=mock_db, redis_manager=mock_redis)

    mock_bot = MagicMock(spec=Bot)
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    mock_user = MagicMock()
    mock_user.username = "test_matching_bot"
    mock_user.id = 123456789
    mock_bot.get_me = AsyncMock(return_value=mock_user)

    # Trigger startup hook
    await dp.emit_startup(bot=mock_bot)
    mock_db.validate_connection.assert_awaited_once()
    mock_redis.validate_connection.assert_awaited_once()

    # Trigger shutdown hook - verify dispatcher shutdown does not independently close resources
    # owned by application_lifespan
    await dp.emit_shutdown(bot=mock_bot)
    mock_bot.session.close.assert_not_called()
    mock_redis.close.assert_not_called()
    mock_db.close.assert_not_called()
