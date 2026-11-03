"""Application entry point: initialization, lifecycle orchestration, and long polling runner."""

import asyncio
import sys

from pydantic import ValidationError

from src.app.config.settings import get_settings
from src.app.infrastructure.lifecycle import application_lifespan
from src.app.infrastructure.logging.logger import configure_logging, get_logger

logger = get_logger("src.app.main")


async def run_app() -> None:
    """Initialize application resources in strict dependency order and start Telegram polling."""
    # Step 1: Load and validate configuration
    try:
        settings = get_settings()
    except ValidationError as exc:
        sys.stderr.write(f"Configuration error: Invalid or missing required settings:\n{exc}\n")
        sys.exit(1)

    # Step 2: Configure logging
    configure_logging(settings)
    logger.info("Initializing %s in [%s] mode...", settings.APP_NAME, settings.APP_ENV)

    # Steps 3 through 8: Initialize Postgres, verify, initialize Redis, verify, create bot, and poll
    try:
        async with application_lifespan(settings) as ctx:
            logger.info("All dependencies healthy. Starting Telegram long polling...")
            await ctx.dispatcher.start_polling(
                ctx.bot,
                allowed_updates=ctx.dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Received process termination signal.")
    except Exception as exc:
        logger.critical("Application startup or runtime execution failed: %s", exc)
        sys.exit(1)


def main() -> None:
    """Synchronous process entrypoint."""
    try:
        asyncio.run(run_app())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
