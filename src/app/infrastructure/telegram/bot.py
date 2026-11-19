"""Telegram bot and dispatcher factory with lifecycle registration."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import User

from src.app.bot.handlers import get_base_router
from src.app.bot.middlewares.logging import StructuredLoggingMiddleware
from src.app.config.settings import AppEnvironment, Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager

logger = logging.getLogger(__name__)

_STRICT_TELEGRAM_ENVS = {AppEnvironment.PRODUCTION, AppEnvironment.STAGING, "production", "staging"}
MAX_STARTUP_RETRY_AFTER_SECONDS = 5.0


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


async def verify_telegram_bot_identity(
    bot: Bot,
    settings: Settings,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> User | None:
    """Verify BOT_TOKEN via Telegram Bot API get_me() with bounded transient retry.

    - Fails fast on TelegramUnauthorizedError in staging/production (never retries bad tokens).
    - Retries transient network/rate-limit errors up to max_attempts in staging/production.
    - Logs warnings without aborting offline development or test runs.
    """
    strict = settings.APP_ENV in _STRICT_TELEGRAM_ENVS
    attempts = max(1, max_attempts) if strict else 1

    for attempt in range(1, attempts + 1):
        try:
            bot_info = await bot.get_me()
            logger.info(
                "Connected to Telegram API as @%s (id=%s)",
                bot_info.username,
                bot_info.id,
            )
            return bot_info
        except TelegramUnauthorizedError:
            logger.error(
                "Telegram Bot API rejected BOT_TOKEN (Unauthorized). Verify configured token."
            )
            if strict:
                raise
            return None
        except TelegramRetryAfter as exc:
            delay = float(getattr(exc, "retry_after", 0) or 0)
            if not strict:
                logger.warning(
                    "Could not verify bot token with Telegram API (rate-limited): %s",
                    exc,
                )
                return None
            if attempt < attempts and 0 < delay <= MAX_STARTUP_RETRY_AFTER_SECONDS:
                logger.warning(
                    "Telegram get_me() rate-limited (attempt %d/%d); retrying in %.2fs",
                    attempt,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
        except (TelegramNetworkError, TimeoutError, OSError) as exc:
            if not strict:
                logger.warning(
                    "Could not verify bot token with Telegram API (offline or test token): %s",
                    exc,
                )
                return None
            if attempt < attempts:
                delay = retry_delay_seconds * attempt
                logger.warning(
                    "Transient Telegram API connectivity error during get_me() "
                    "(attempt %d/%d, %s); retrying in %.2fs",
                    attempt,
                    attempts,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.error(
                "Telegram Bot API get_me() unreachable after %d attempts (%s).",
                attempts,
                type(exc).__name__,
            )
            raise
        except Exception as exc:
            logger.warning(
                "Could not verify bot token with Telegram API (%s): %s",
                type(exc).__name__,
                exc,
            )
            if strict:
                raise
            return None

    return None


def create_dispatcher(
    settings: Settings,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
) -> Dispatcher:
    """Create and configure the aiogram Dispatcher with lifecycle handlers."""
    dp = Dispatcher()

    # Register dependencies in dispatcher workflow data for clean handler injection
    dp["settings"] = settings
    if db_manager is not None:
        dp["db_manager"] = db_manager
    if redis_manager is not None:
        dp["redis_manager"] = redis_manager

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

        # Verify bot token with Telegram servers (strict in staging and production)
        await verify_telegram_bot_identity(bot, settings)

        logger.info("Application startup lifecycle completed successfully.")

    @dp.shutdown()
    async def on_shutdown(bot: Bot) -> None:
        logger.info("Shutting down Telegram Bot application gracefully...")
        # Note: Resource lifecycle (Bot session, RedisManager, and DatabaseManager cleanup)
        # is authoritatively owned by application_lifespan to guarantee single-owner,
        # exactly-once teardown.
        logger.info("Application shutdown hook completed.")

    return dp
