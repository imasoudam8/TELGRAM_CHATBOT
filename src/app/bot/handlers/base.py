"""Base bot handlers providing core application routing."""

import logging

from aiogram import Router
from aiogram.types import Message

from src.app.bot.handlers.onboarding import get_onboarding_router

logger = logging.getLogger(__name__)


async def handle_start_smoke_test(message: Message) -> None:
    """Handle /start command strictly as a connectivity verification smoke test.

    Retained for backward-compatibility in diagnostic tests.
    """
    user_id = message.from_user.id if message.from_user else "unknown"
    logger.info("Received /start smoke test from user %s", user_id)

    await message.answer(
        "👋 <b>System Online</b>\n\n"
        "Anonymous Matching Bot service is running in health-check mode.\n"
        "Core infrastructure and network connectivity are operational."
    )


def get_base_router() -> Router:
    """Factory creating the primary application router."""
    router = Router(name="base")
    router.include_router(get_onboarding_router())
    return router


# Module-level alias for convenience
base_router = get_base_router()
