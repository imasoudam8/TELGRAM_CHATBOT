"""Application entry point: initialization, lifecycle orchestration, and long polling runner."""

import asyncio
import contextlib
import signal
import sys

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

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

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal(sig_name: str) -> None:
        logger.info("Received %s signal; initiating graceful shutdown...", sig_name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, sig.name)

    # Steps 3 through 8: Initialize Postgres, verify migrations, initialize Redis, start polling
    try:
        async with application_lifespan(settings) as ctx:
            logger.info("All dependencies healthy. Starting Telegram long polling...")
            polling_task = asyncio.create_task(
                ctx.dispatcher.start_polling(
                    ctx.bot,
                    allowed_updates=ctx.dispatcher.resolve_used_update_types(),
                    close_bot_session=False,
                    handle_signals=False,
                ),
                name="telegram-polling",
            )
            stop_waiter = asyncio.create_task(shutdown_event.wait(), name="shutdown-waiter")

            done, _ = await asyncio.wait(
                {polling_task, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_waiter in done and not polling_task.done():
                logger.info("Stopping Telegram dispatcher polling...")
                with contextlib.suppress(RuntimeError):
                    await ctx.dispatcher.stop_polling()
                try:
                    await asyncio.wait_for(polling_task, timeout=15.0)
                except TimeoutError:
                    logger.warning("Polling task did not finish within 15s; cancelling.")
                    polling_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await polling_task
            else:
                stop_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_waiter
                # Re-raise polling exception if polling failed unexpectedly
                polling_task.result()

    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        logger.info("Application shutdown completed.")
    except (OSError, TimeoutError, SQLAlchemyError, RedisError, RuntimeError) as exc:
        logger.critical(
            "Infrastructure or startup readiness failure: %s",
            exc,
            extra={"error_category": "infrastructure"},
        )
        sys.exit(2)
    except Exception as exc:
        logger.critical(
            "Unhandled application runtime failure: %s",
            exc,
            extra={"error_category": "application"},
        )
        sys.exit(1)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


def main() -> None:
    """Synchronous process entrypoint."""
    try:
        asyncio.run(run_app())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
