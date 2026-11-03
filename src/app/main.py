"""Application entry point: initialization, lifecycle orchestration, and long polling runner."""

import asyncio
import sys

from pydantic import ValidationError

from src.app.config.settings import get_settings
from src.app.infrastructure.database.session import get_database_manager
from src.app.infrastructure.logging.logger import configure_logging, get_logger
from src.app.infrastructure.redis.client import get_redis_manager
from src.app.infrastructure.telegram.bot import create_bot, create_dispatcher

logger = get_logger("src.app.main")


async def run_app() -> None:
    """Initialize application resources, attach signal handlers, and start Telegram long polling."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Direct clean message to stderr without printing stack traces containing secret values
        sys.stderr.write(f"Configuration error: Invalid or missing required settings:\n{exc}\n")
        sys.exit(1)

    configure_logging(settings)
    logger.info("Initializing %s in [%s] mode...", settings.APP_NAME, settings.APP_ENV)

    # Initialize infrastructure managers
    db_manager = get_database_manager(settings)
    redis_manager = get_redis_manager(settings)

    # Initialize bot and dispatcher
    try:
        bot = create_bot(settings)
        dp = create_dispatcher(settings, db_manager=db_manager, redis_manager=redis_manager)
    except Exception as exc:
        logger.critical("Failed to instantiate bot or dispatcher: %s", exc)
        sys.exit(1)

    # Start long polling
    logger.info("Starting Telegram long polling...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=True,
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Received process termination signal.")
    except Exception as exc:
        logger.critical("Unexpected runtime exception in bot polling loop: %s", exc, exc_info=True)
        sys.exit(1)


def main() -> None:
    """Synchronous process entrypoint."""
    try:
        asyncio.run(run_app())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
