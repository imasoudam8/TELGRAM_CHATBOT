"""Middleware for structured, privacy-preserving event logging."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

logger = logging.getLogger(__name__)


class StructuredLoggingMiddleware(BaseMiddleware):
    """Logs incoming Telegram update events without recording
    sensitive payload or message contents.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        start_time = time.monotonic()
        event_type = type(event).__name__
        update_id = getattr(event, "update_id", None) if isinstance(event, Update) else None

        logger.debug(
            "Processing update event",
            extra={"event_type": event_type, "update_id": update_id},
        )

        try:
            result = await handler(event, data)
            elapsed = time.monotonic() - start_time
            logger.debug(
                "Completed update event",
                extra={"event_type": event_type, "duration_ms": round(elapsed * 1000, 2)},
            )
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.error(
                "Unhandled error in update handler: %s",
                exc,
                exc_info=True,
                extra={"event_type": event_type, "duration_ms": round(elapsed * 1000, 2)},
            )
            raise
