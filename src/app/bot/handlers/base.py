"""Base bot handlers providing core application routing."""

import logging

from aiogram import Router
from aiogram.types import Message

from src.app.bot.handlers.chat import get_chat_router
from src.app.bot.handlers.connections import get_connections_router
from src.app.bot.handlers.direct_requests import get_direct_requests_router
from src.app.bot.handlers.discovery import get_discovery_router
from src.app.bot.handlers.matching import get_matching_router
from src.app.bot.handlers.onboarding import get_onboarding_router
from src.app.bot.handlers.profile import get_profile_router
from src.app.bot.handlers.wallet import get_wallet_router

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
    router.include_router(get_chat_router())
    router.include_router(get_profile_router())
    router.include_router(get_discovery_router())
    router.include_router(get_matching_router())
    router.include_router(get_direct_requests_router())
    router.include_router(get_connections_router())
    router.include_router(get_wallet_router())
    return router


# Module-level alias for convenience
base_router = get_base_router()
