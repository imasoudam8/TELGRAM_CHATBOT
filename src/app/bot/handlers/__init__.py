"""Bot handlers package."""

from src.app.bot.handlers.base import base_router, get_base_router, handle_start_smoke_test
from src.app.bot.handlers.chat import get_chat_router
from src.app.bot.handlers.connections import get_connections_router
from src.app.bot.handlers.direct_requests import get_direct_requests_router
from src.app.bot.handlers.discovery import get_discovery_router
from src.app.bot.handlers.matching import get_matching_router
from src.app.bot.handlers.onboarding import (
    get_onboarding_router,
    handle_age_confirm_callback,
    handle_age_decline_callback,
    handle_main_menu_entry,
    handle_start,
)
from src.app.bot.handlers.profile import get_profile_router, handle_profile_menu_entry
from src.app.bot.handlers.wallet import get_wallet_router

__all__ = [
    "base_router",
    "get_base_router",
    "get_chat_router",
    "get_connections_router",
    "get_direct_requests_router",
    "get_discovery_router",
    "get_matching_router",
    "get_onboarding_router",
    "get_profile_router",
    "get_wallet_router",
    "handle_age_confirm_callback",
    "handle_age_decline_callback",
    "handle_main_menu_entry",
    "handle_profile_menu_entry",
    "handle_start",
    "handle_start_smoke_test",
]
