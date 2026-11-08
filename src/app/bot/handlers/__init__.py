"""Bot handlers package."""

from src.app.bot.handlers.base import base_router, get_base_router, handle_start_smoke_test
from src.app.bot.handlers.onboarding import (
    get_onboarding_router,
    handle_age_confirm_callback,
    handle_age_decline_callback,
    handle_main_menu_entry,
    handle_start,
)

__all__ = [
    "base_router",
    "get_base_router",
    "get_onboarding_router",
    "handle_age_confirm_callback",
    "handle_age_decline_callback",
    "handle_main_menu_entry",
    "handle_start",
    "handle_start_smoke_test",
]
