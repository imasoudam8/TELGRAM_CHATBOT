"""Application lifecycle management: ordered startup, dependency validation, and teardown."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiogram import Bot, Dispatcher

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.infrastructure.redis.client import RedisManager, get_redis_manager
from src.app.infrastructure.telegram.bot import create_bot, create_dispatcher

logger = logging.getLogger(__name__)


@dataclass
class ApplicationContext:
    """Encapsulates running application services, clients, and dispatcher."""

    settings: Settings
    db_manager: DatabaseManager
    redis_manager: RedisManager
    bot: Bot
    dispatcher: Dispatcher


@asynccontextmanager
async def application_lifespan(
    settings: Settings,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
) -> AsyncGenerator[ApplicationContext, None]:
    """Orchestrates strict startup ordering, health validation, and graceful teardown.

    Lifecycle stages:
      1. Initialize PostgreSQL connection pool.
      2. Validate PostgreSQL connectivity ('SELECT 1').
      3. Initialize Redis connection pool.
      4. Validate Redis connectivity ('PING').
      5. Create Telegram Bot client and Dispatcher.
      6. Yield ApplicationContext to start polling once dependencies are verified healthy.
      7. On startup failure or shutdown: clean up all initialized resources in reverse order.
    """
    active_db: DatabaseManager | None = None
    active_redis: RedisManager | None = None
    active_bot: Bot | None = None

    try:
        # Step 1 & 2: PostgreSQL initialization and connectivity verification
        logger.info("Initializing PostgreSQL database...")
        active_db = db_manager if db_manager is not None else get_database_manager(settings)
        await active_db.initialize()

        logger.info("Verifying PostgreSQL connectivity...")
        await active_db.validate_connection()
        logger.info("PostgreSQL is verified healthy.")

        # Step 3 & 4: Redis initialization and connectivity verification
        logger.info("Initializing Redis cache and coordination client...")
        active_redis = redis_manager if redis_manager is not None else get_redis_manager(settings)
        await active_redis.initialize()

        logger.info("Verifying Redis connectivity...")
        await active_redis.validate_connection()
        logger.info("Redis is verified healthy.")

        # Step 5: Telegram Bot and Dispatcher setup
        logger.info("Creating Telegram Bot client and Dispatcher...")
        active_bot = create_bot(settings)
        dispatcher = create_dispatcher(
            settings=settings,
            db_manager=active_db,
            redis_manager=active_redis,
        )

        context = ApplicationContext(
            settings=settings,
            db_manager=active_db,
            redis_manager=active_redis,
            bot=active_bot,
            dispatcher=dispatcher,
        )

        yield context

    finally:
        logger.info("Initiating graceful teardown of initialized application resources...")

        # 1. Close Telegram Bot session
        if active_bot is not None and active_bot.session is not None:
            try:
                await active_bot.session.close()
                logger.info("Telegram Bot session closed.")
            except Exception as exc:
                logger.error("Error closing Telegram bot session: %s", exc)

        # 2. Close Redis client and pool
        if active_redis is not None:
            try:
                await active_redis.close()
                logger.info("Redis connection pool closed.")
            except Exception as exc:
                logger.error("Error closing Redis client: %s", exc)

        # 3. Dispose PostgreSQL engine
        if active_db is not None:
            try:
                await active_db.close()
                logger.info("PostgreSQL connection pool disposed.")
            except Exception as exc:
                logger.error("Error disposing PostgreSQL connection pool: %s", exc)

        logger.info("All application resources safely released.")
