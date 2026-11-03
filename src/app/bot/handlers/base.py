"""Base bot handlers providing minimal connectivity smoke test."""

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

logger = logging.getLogger(__name__)


async def handle_start_smoke_test(message: Message) -> None:
    """Handle /start command strictly as a connectivity verification smoke test.

    No user registration, database record creation, or state persistence is performed.
    """
    user_id = message.from_user.id if message.from_user else "unknown"
    logger.info("Received /start smoke test from user %s", user_id)

    await message.answer(
        "👋 <b>System Online</b>\n\n"
        "Anonymous Matching Bot service is running in health-check mode.\n"
        "Core infrastructure and network connectivity are operational."
    )


def get_base_router() -> Router:
    """Factory creating a new base router instance."""
    router = Router(name="base")
    router.message.register(handle_start_smoke_test, CommandStart())
    return router


# Module-level alias for convenience
base_router = get_base_router()
