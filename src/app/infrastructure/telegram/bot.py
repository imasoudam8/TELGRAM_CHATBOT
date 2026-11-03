"""Telegram bot and dispatcher factory with lifecycle registration."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.app.bot.handlers import get_base_router
from src.app.bot.middlewares.logging import StructuredLoggingMiddleware
from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager

logger = logging.getLogger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Instantiate a Telegram Bot instance with HTML parse mode and validated token."""
    raw_token = settings.BOT_TOKEN.get_secret_value()
    return Bot(
        token=raw_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def setup_routers(dp: Dispatcher) -> None:
    """Register all modular application routers with the dispatcher."""
    dp.include_router(get_base_router())
    logger.debug("Base router registered with dispatcher.")


def setup_middlewares(dp: Dispatcher) -> None:
    """Register application middlewares with the dispatcher."""
    dp.update.outer_middleware(StructuredLoggingMiddleware())
    logger.debug("Structured logging middleware registered.")


def create_dispatcher(
    settings: Settings,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
) -> Dispatcher:
    """Create and configure the aiogram Dispatcher with lifecycle handlers."""
    dp = Dispatcher()

    setup_middlewares(dp)
    setup_routers(dp)

    @dp.startup()
    async def on_startup(bot: Bot) -> None:
        logger.info("Starting Telegram Bot application [%s]...", settings.APP_NAME)
        logger.info("Environment: %s | Timezone: %s", settings.APP_ENV, settings.TIMEZONE)

        # Validate database connectivity
        if db_manager is not None:
            logger.info("Validating database connection on startup...")
            await db_manager.validate_connection()

        # Validate Redis connectivity
        if redis_manager is not None:
            logger.info("Validating Redis connection on startup...")
            await redis_manager.validate_connection()

        # In production or staging, verify bot token with Telegram servers
        try:
            bot_info = await bot.get_me()
            logger.info("Connected to Telegram API as @%s (id=%s)", bot_info.username, bot_info.id)
        except Exception as exc:
            # In test environments with dummy tokens or offline containers, log notice
            logger.warning(
                "Could not verify bot token with Telegram API (offline or test token): %s", exc
            )
            if settings.APP_ENV in {"production", "staging"}:
                raise

        logger.info("Application startup lifecycle completed successfully.")

    @dp.shutdown()
    async def on_shutdown(bot: Bot) -> None:
        logger.info("Shutting down Telegram Bot application gracefully...")

        # Close Telegram bot session
        try:
            await bot.session.close()
            logger.info("Telegram Bot session closed.")
        except Exception as exc:
            logger.error("Error closing Telegram bot session: %s", exc)

        # Disconnect Redis
        if redis_manager is not None:
            try:
                await redis_manager.close()
            except Exception as exc:
                logger.error("Error closing Redis manager: %s", exc)

        # Dispose database engine
        if db_manager is not None:
            try:
                await db_manager.close()
            except Exception as exc:
                logger.error("Error closing Database manager: %s", exc)

        logger.info("Application shutdown completed.")

    return dp
